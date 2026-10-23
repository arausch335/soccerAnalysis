from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from collection.seasonRunner import SeasonRunner
from collection.ensembleProgress import PortableProgress
from collection.jobCompletion import assess
from providers.identityReview import ReviewStore
from providers.matchComparison import provider_error
from storage import PandasStore
from tests.test_providers import Sofa,Fot,fot_payload
from tests.test_job_completion import CompletionTests


def quiet(*a,**k):pass


class EvidenceTests(unittest.TestCase):
    def collect(self,root,*,starter_conflict=False):
        class Conflict(Fot):
            def match_details(self,mid):
                payload=fot_payload(mid)
                if starter_conflict:
                    team=payload['content']['lineup']['homeTeam'];team['subs']=team['starters'];team['starters']=[]
                else:payload['header']['teams'][0]['score']=3
                return payload
        directory=root/'premier-league/2024-2025'
        result=SeasonRunner({'sofascore':Sofa(),'fotmob':Conflict()},directory,providers=['sofascore','fotmob'],spatial=(),report=quiet).run('EPL','2024/25',limit=1,build_csvs=False)
        match=PandasStore(directory/'match_checkpoints/1').from_csv().matches[1]
        return result,match

    def test_score_failure_reaches_inventory_and_main_with_both_provider_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);result,match=self.collect(root)
            self.assertEqual(result.status.tolist(),['partial'])
            self.assertEqual(result.iloc[0].error,'fotmob: match score mismatch')
            comparison=match.metadata['provider_comparisons']['fotmob']
            self.assertEqual(comparison['baseline']['home_score'],0)
            self.assertEqual(comparison['source']['home_score'],3)
            progress=PortableProgress(None,root,worker_id='msi');progress.record_match(match)
            records=progress.local_records
            fixture=CompletionTests();fixture.setUp()
            job=replace(fixture.job,season='2024/2025')
            schedules=[{'kind':'combined','competition':'Premier League','season':'2024-2025','scope_version':2,'match_ids':['1']}]
            summary=assess(job,records,schedules,{job.id:{'state':'paused'}})
            self.assertEqual(summary['review'][0]['provider_comparisons']['fotmob'],comparison)
            self.assertEqual(summary['review'][0]['baseline_fixture']['match_id'],'1')

    def test_starter_conflict_reports_the_known_player_without_inventing_an_identity_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);result,match=self.collect(root,starter_conflict=True)
            self.assertEqual(result.status.tolist(),['partial'])
            rows=match.metadata['identity_mismatches']['fotmob'];self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['baseline_player_name'],'Player One')
            self.assertEqual(rows[0]['provider_player_name'],'Player One')
            self.assertTrue(rows[0]['baseline_starter']);self.assertFalse(rows[0]['provider_starter'])
            self.assertIn('starter mismatch',rows[0]['issue'])
            self.assertEqual(ReviewStore(root).snapshot()['pending'],[])
            progress=PortableProgress(None,root,worker_id='msi');progress.record_match(match)
            evidence=next(iter(progress.local_records.values()))['evidence']
            self.assertEqual(evidence['identity_mismatches']['fotmob'],rows)

    def test_provider_prefix_is_present_only_once(self):
        self.assertEqual(provider_error('whoscored','whoscored: whoscored: match score mismatch'),'whoscored: match score mismatch')

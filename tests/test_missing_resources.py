from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock

from collection.ensemble import merged_checkpoint
from collection.ensembleProgress import PortableProgress,progress_session,job_key
from collection.seasonRunner import SeasonRunner
from collection.workInstructions import remaining_scope,remaining_resources
from collection.workerIdentity import ensure_identity
from providers import SofaScoreLoader,ProviderRequestError
from storage.pandasStore import PandasStore,atomic_json
from tests.test_providers import Sofa,Fot,event,lineup


class MissingResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'data'
        atomic_json(self.root/'.soccer-scraper.json',{'format':1});ensure_identity(self.root,'dario')
        self.directory=self.root/'premier-league/2024-2025'
        self.sofa=Sofa();self.sofa.count=1
        self.sofa.statistics=Mock(return_value={'statistics':[]})
        self.sofa.incidents=Mock(return_value={'incidents':[]})
        self.fot=Fot();self.who=Mock()
        self.runner=SeasonRunner({'sofascore':self.sofa,'fotmob':self.fot,'whoscored':self.who},
            self.directory,spatial=(),report=lambda *a,**k:None)
        self.progress=PortableProgress(self.base/'ensemble/provenance.json',self.root,worker_id='dario')
        self.job=SimpleNamespace(competition='Premier League',as_dict=lambda:{'season':'2024-2025'})

    @contextmanager
    def scope(self,wanted):
        with remaining_scope(self.job,{'schedule_known':True,'configuration':'test','remaining':{'1':wanted}}),progress_session(self.progress):
            yield

    def run_missing(self,wanted):
        with self.scope(wanted):
            return self.runner.run('Premier League','2024/25',build_csvs=False)

    def test_details_only_never_fetches_lineups_players_or_other_providers(self):
        result=self.run_missing(['match_statistics','incidents'])
        self.assertEqual(result.status.tolist(),['complete'])
        self.assertEqual(self.sofa.calls,[]);self.assertEqual(self.fot.calls,[])
        self.who.seasons.assert_not_called();self.who.match_details.assert_not_called()
        self.sofa.statistics.assert_called_once_with(1);self.sofa.incidents.assert_called_once_with(1)
        row=self.progress.local_records[job_key('sofascore','Premier League','2024/25',1)]
        self.assertEqual(row['resources']['incidents'],'complete')
        self.assertEqual(row['resources']['match_statistics'],'complete')
        self.assertEqual(row['resources']['sofascore_stats'],'not_attempted')
        self.assertEqual(row['resources']['fotmob_enrichment'],'not_attempted')
        self.assertNotIn('provider:sofascore',row['capabilities'])
        self.assertNotIn('browser_details',row['capabilities'])
        self.sofa.statistics.reset_mock();self.sofa.incidents.reset_mock()
        self.run_missing(['match_statistics','incidents'])
        self.sofa.statistics.assert_not_called();self.sofa.incidents.assert_not_called()

    def test_patch_merge_retains_original_player_stats_and_provider_metadata(self):
        self.run_missing(['match_statistics','incidents'])
        loader=SofaScoreLoader(None);season=loader.select_season(17,'Premier League',61627,'Premier League 24/25')
        old=loader._build_match(season,event(),lineup())
        old.metadata['provider_status']={'sofascore':'complete','fotmob':'complete','whoscored':'complete'}
        before=old.home_lineup.players[0].stats.raw.copy()
        target=self.base/'existing';PandasStore(target).to_csv(loader.dataset)
        staged=self.base/'merged'
        merged_checkpoint(self.directory/'match_checkpoints/1',target,staged)
        merged=PandasStore(staged).from_csv().matches[1]
        self.assertEqual(merged.home_lineup.players[0].stats.raw['totalPass'],before['totalPass'])
        self.assertEqual(len(merged.home_lineup.players),1)
        self.assertEqual(merged.metadata['provider_status'],old.metadata['provider_status'])
        self.assertEqual(merged.metadata['browser_details']['coverage']['incidents'],'complete')
        self.assertEqual(PandasStore(target).from_csv().matches[1].metadata,old.metadata)

    def test_mixed_gap_fetches_only_requested_provider_with_supporting_lineups(self):
        result=self.run_missing(['fotmob_enrichment','incidents'])
        self.assertEqual(result.status.tolist(),['complete'])
        self.assertEqual(self.fot.calls,[101]);self.assertEqual(self.sofa.calls,[1])
        self.sofa.statistics.assert_not_called();self.who.seasons.assert_not_called()
        row=self.progress.local_records[job_key('sofascore','Premier League','2024/25',1)]
        self.assertEqual(row['resources']['fotmob_enrichment'],'complete')
        self.assertEqual(row['resources']['sofascore_stats'],'not_attempted')

    def test_missing_whoscored_reuses_local_raw_events_without_requesting_completed_providers(self):
        from tests.test_whoscored_integration import populate
        populate(self.root)
        result=self.run_missing(['whoscored_stats','whoscored_events','incidents'])
        self.assertEqual(result.status.tolist(),['complete'])
        self.assertEqual(self.sofa.calls,[1]);self.assertEqual(self.fot.calls,[])
        self.sofa.statistics.assert_not_called();self.who.match_details.assert_not_called()
        events=self.progress.local_records[job_key('whoscored_events','Premier League','2024/25',11)]
        self.assertEqual(events['resources']['whoscored_events'],'complete')

    def test_player_only_extension_pass_does_not_fetch_team_details_and_reuses_saved_players(self):
        from providers.transports.sofascore_extension import ExtensionSofaScoreClient
        from tests.test_extension_client import FakeBridge
        from tests.test_extension_payloads import extension_settings
        bridge=FakeBridge()
        self.runner.clients['sofascore']=ExtensionSofaScoreClient(self.root,extension_settings(),bridge=bridge,report=lambda *a,**k:None)
        self.runner.spatial_clients=(self.runner.clients['sofascore'],)
        self.assertEqual(self.run_missing(['sofascore_stats']).status.tolist(),['complete'])
        self.assertIn('event/1/player/7/statistics',bridge.reads)
        self.assertNotIn('event/1/statistics',bridge.reads)
        self.assertNotIn('event/1/incidents',bridge.reads)
        row=self.progress.local_records[job_key('sofascore','Premier League','2024/25',1)]
        self.assertEqual(row['resources']['sofascore_stats'],'complete')
        self.assertEqual(row['resources']['match_statistics'],'not_attempted')
        bridge.reads.clear()
        self.assertEqual(self.run_missing(['sofascore_stats']).status.tolist(),['complete'])
        self.assertEqual(bridge.reads,[])

    def test_failed_resource_is_not_certified_and_saved_success_is_reused(self):
        self.sofa.incidents.side_effect=ProviderRequestError('rate limited',status_code=429)
        self.assertEqual(self.run_missing(['match_statistics','incidents']).status.tolist(),['partial'])
        row=self.progress.local_records[job_key('sofascore','Premier League','2024/25',1)]
        self.assertEqual(row['resources']['match_statistics'],'complete')
        self.assertEqual(row['resources']['incidents'],'failed')
        self.sofa.statistics.reset_mock();self.sofa.incidents.side_effect=None;self.runner._sofascore_blocked=False
        self.assertEqual(self.run_missing(['match_statistics','incidents']).status.tolist(),['complete'])
        self.sofa.statistics.assert_not_called();self.assertEqual(self.sofa.calls,[])

    def test_resource_scope_is_season_bound_and_audits_keep_full_collection(self):
        with self.scope(['incidents']):
            self.assertEqual(remaining_resources('Premier League','2024-2025'),{'1':['incidents']})
            self.assertIsNone(remaining_resources('Bundesliga','2024-2025'))
        self.assertIsNone(remaining_resources('Premier League','2024-2025'))
        with remaining_scope(self.job,{'schedule_known':True,'configuration':'test','audit_requested':True,'targets':['1'],'remaining':{}}):
            self.assertIsNone(remaining_resources('Premier League','2024-2025'))


if __name__=='__main__':unittest.main()

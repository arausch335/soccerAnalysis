import csv
from pathlib import Path
import tempfile
import unittest

from fixtures.identityRefresh import reconcile_exports, resolve_rows, read_table
from providers import reep
from storage.pandasStore import atomic_json


def bridge(*rows,redirects=None):
    rows=[list(r) for r in rows]
    return {'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows,'release':'test','redirects':redirects or {}}


class IdentityRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.output=Path(self.tmp.name)/'dataset'
        self.value=bridge(('match','sofascore','1','rm1'),('match','fotmob','10','rm1'),
                          ('match','whoscored','100','rm1'),('team','sofascore','2','rt2'),
                          ('team','whoscored','200','rt2'))
        reep.install(self.root,self.value)

    def write(self,name,rows):
        path=self.output/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        return path

    def row(self,**kwargs):
        return dict(sofascore_id='1',fotmob_id='',whoscored_id='',reep_id='',identity_status='unmapped',**kwargs)

    def test_fills_published_ids_without_overwriting_native_ids(self):
        rows,review=resolve_rows([self.row()],reep.Index(self.value),'match')
        self.assertEqual((rows[0]['fotmob_id'],rows[0]['whoscored_id'],rows[0]['reep_id']),('10','100','rm1'))
        self.assertFalse(review)
        original=self.row();original['fotmob_id']='99'
        result,_=resolve_rows([original],reep.Index(self.value),'match')
        self.assertEqual(result[0]['fotmob_id'],'99')

    def test_conflicting_published_bridges_are_reviewed_without_filling_ids(self):
        value=bridge(*self.value['rows'],('match','fotmob','99','rm2'))
        original=self.row();original['fotmob_id']='99'
        rows,review=resolve_rows([original],reep.Index(value),'match')
        self.assertEqual(rows[0]['identity_status'],'conflict')
        self.assertEqual(rows[0]['whoscored_id'],'')
        self.assertEqual(review[0]['reason'],'published_bridge_conflict')

    def test_old_review_conflicts_remain_unresolved(self):
        original=self.row();original['identity_status']='conflict'
        rows,review=resolve_rows([original],reep.Index(self.value),'match')
        self.assertEqual(rows,[original]);self.assertEqual(review[0]['reason'],'existing_identity_conflict')

    def test_a_provider_match_cannot_be_filled_for_two_baseline_matches(self):
        value=bridge(*self.value['rows'],('match','sofascore','3','rm1'))
        other=self.row();other['sofascore_id']='3'
        rows,review=resolve_rows([self.row(),other],reep.Index(value),'match')
        self.assertTrue(all(r['whoscored_id']=='' and r['reep_id']=='' for r in rows))
        self.assertTrue(all(r['identity_status']=='conflict' for r in rows))
        self.assertTrue(any(r['reason']=='provider_fixture_reused' for r in review))

    def test_only_published_redirects_can_replace_existing_canonical_ids(self):
        original=self.row();original['reep_id']='old'
        rows,review=resolve_rows([original],reep.Index(self.value),'match')
        self.assertEqual(rows[0]['reep_id'],'old');self.assertEqual(rows[0]['identity_status'],'conflict')
        value={**self.value,'redirects':{'old':'middle','middle':'rm1'}}
        rows,review=resolve_rows([original],reep.Index(value),'match')
        self.assertEqual(rows[0]['reep_id'],'rm1');self.assertEqual(review,[])

    def test_compiled_exports_refresh_without_raw_data_and_preserve_schedule_gaps(self):
        fixture=dict(competition='League',season='2024-2025',date='2024-08-01',home_team='Home',away_team='Away',
                     sofascore_match_id='1',fotmob_match_id='',whoscored_match_id='',reep_match_id='',
                     match_identity_status='unmapped',fotmob_fixture_check='missing',whoscored_fixture_check='missing',
                     fotmob_match_method='',whoscored_match_method='')
        path=self.write('fixtures.csv',[fixture]);original=path.read_bytes()
        self.write('concordance/match_ids.csv',[self.row()])
        self.write('fixture_concordance/match_ids.csv',[dict(competition='League',season='2024-2025',date='2024-08-01',
                   home_team_name='Home',away_team_name='Away',**self.row())])
        atomic_json(self.output/'fixture_concordance/manifest.json',{'schedule_coverage_complete':False})
        report=reconcile_exports(self.root,self.output,dry_run=True)
        self.assertEqual(report['tables']['fixtures']['after']['present']['whoscored'],1)
        self.assertEqual(path.read_bytes(),original)
        reconcile_exports(self.root,self.output)
        _,rows=read_table(path)
        self.assertEqual(rows[0]['whoscored_match_id'],'100')
        self.assertEqual(rows[0]['whoscored_fixture_check'],'bridge_only')
        import json
        manifest=json.loads((self.output/'fixture_concordance/manifest.json').read_text())
        self.assertFalse(manifest['schedule_coverage_complete'])
        self.assertFalse(manifest['all_provider_fixtures_validated'])
        self.assertEqual((self.root/'_system/identity/previous_exports/fixtures.csv').read_bytes(),original)
        self.assertEqual(reconcile_exports(self.root,self.output,dry_run=True)['changed_files'],0)

    def test_rejected_fixture_evidence_cannot_be_replaced_by_a_bridge(self):
        row=dict(sofascore_match_id='1',fotmob_match_id='',whoscored_match_id='',reep_match_id='',
                 whoscored_fixture_check='reep_fixture_failed_validation',match_identity_status='unmapped')
        rows,_=resolve_rows([row],reep.Index(self.value),'match',suffix='_match_id',status='match_identity_status')
        self.assertEqual(rows[0]['whoscored_match_id'],'')

    def test_superseded_export_row_is_removed_and_played_match_is_retried(self):
        from tests.test_fixture_dataset import event
        from dataset_builder import fixtures_from_events
        values=bridge(*self.value['rows'],('match','sofascore','3','rm1'))
        reep.install(self.root,values)
        old={**event(1),'status':{'type':'postponed'}};played=event(3,day=old['startTimestamp']+86400)
        rows=[]
        for e in [old,played]:
            row={k:'' if v is None else str(v) for k,v in fixtures_from_events([e],'Premier League','2010-2011').iloc[0].to_dict().items()}
            row.update(sofascore_match_id=str(e['id']),sofascore_home_team_id='1',sofascore_away_team_id='2',
                       fotmob_match_id='',whoscored_match_id='',reep_match_id='',match_identity_status='conflict',
                       fotmob_fixture_check='missing',whoscored_fixture_check='missing',fotmob_match_method='',whoscored_match_method='')
            rows.append(row)
        self.write('fixtures.csv',rows)
        self.write('concordance/reconciliation/review.csv',[dict(kind='match',sofascore_id=str(i),reason='provider_fixture_reused',provider='reep',candidate_id='rm1') for i in [1,3]])
        self.write('concordance/match_ids.csv',[{**self.row(),'sofascore_id':str(i)} for i in [1,3]])
        report=reconcile_exports(self.root,self.output)
        self.assertEqual(report['superseded_fixtures_removed'],1)
        _,result=read_table(self.output/'fixtures.csv')
        self.assertEqual(len(result),1);self.assertEqual(result[0]['sofascore_match_id'],'3')
        self.assertEqual(result[0]['whoscored_match_id'],'100');self.assertNotEqual(result[0]['match_identity_status'],'conflict')
        _,matches=read_table(self.output/'concordance/match_ids.csv')
        self.assertEqual([r['sofascore_id'] for r in matches],['3'])

    def test_saved_primary_periods_repair_scores_and_revalidate_the_fixture(self):
        from fixtures.scoreRepair import repair_exported_scores
        from tests.test_fixture_dataset import event,DAY
        source=event(1)
        source['homeScore']={'current':2,'normaltime':1,'overtime':1}
        source['awayScore']={'current':3,'normaltime':3,'overtime':0}
        atomic_json(self.root/'_system/identity/fixture_score_evidence.json',{'format':'soccer-fixture-score-evidence-v1','events':[source]})
        peer=event(10,10,20);peer['homeScore']={'current':2};peer['awayScore']={'current':3}
        atomic_json(self.root/'_system/fixtures/fotmob/premier-league/2010-2011.json',{'events':[peer]})
        row=dict(sofascore_match_id='1',sofascore_home_team_id='1',sofascore_away_team_id='2',home_team='Home',away_team='Away',
                 competition='Premier League',season='2010-2011',date='2010-09-01',status='finished',home_score='1',away_score='0',
                 match_identity_status='conflict',fotmob_match_id='',whoscored_match_id='',fotmob_fixture_check='reep_fixture_failed_validation',whoscored_fixture_check='bridge_only')
        repaired,audit=repair_exported_scores([row],self.root,reep.index(self.root))
        self.assertEqual((repaired[0]['home_score'],repaired[0]['away_score']),('2','3'))
        self.assertEqual(repaired[0]['fotmob_fixture_check'],'validated')
        self.assertEqual(repaired[0]['fotmob_match_id'],'10');self.assertEqual(repaired[0]['match_identity_status'],'unmapped')
        self.assertEqual(len(audit),1)


if __name__=='__main__':unittest.main()

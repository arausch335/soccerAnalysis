from pathlib import Path
import tempfile
import unittest

from providers import reep
from providers.identity import teams_match
from fixtures.dataset import concord
from fixtures.scoreRepair import repair_exported_scores
from fixtures.builder import FixtureCompetition
from storage.pandasStore import atomic_json
from tests.test_fixture_dataset import event,DAY
from providers.teamProfiles import canonical_record,canonical_team_id


class ReviewedClubProfiles(unittest.TestCase):
    def test_canonical_choices_are_consistent_and_provider_scoped(self):
        for sid,fid,wid,name,expected_f,expected_w in [('2742','9880','265','AC Cesena','584022','19415'),
                ('3275','10127','697','CSKA Sofia','10144','1275'),
                ('37426','210132','9425','FC Viitorul Constanța','210132','9425')]:
            row=canonical_record({'sofascore_id':sid,'fotmob_id':fid,'whoscored_id':wid,'team_name':name},reference=True)
            self.assertEqual(row['fotmob_id'],expected_f);self.assertEqual(row['whoscored_id'],expected_w)
            self.assertEqual(row['sofascore_id'],'3294' if sid=='37426' else sid)
            self.assertEqual(row['team_name'],'Farul Constanța' if sid=='37426' else 'Cesena' if sid=='2742' else name)
        self.assertEqual(canonical_team_id('sofascore','9880'),'9880')
        self.assertEqual(canonical_record({'team_id':'9880','team':'Unrelated'},'sofascore')['team_id'],'9880')

    def test_fixture_generic_identity_is_canonical_while_native_provider_id_remains(self):
        rows=self.rows+[['team','sofascore','3294','rtFarul'],['team','sofascore','37426','rtFarul'],
                        ['team','fotmob','210132','rtFarul'],['team','whoscored','9425','rtFarul']]
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        comp=FixtureCompetition('Champions League',7)
        source=event(101,37426,1,'FC Viitorul Constanța','Other')
        exported,*_=concord([(comp,2010,{'sofascore':[source]})],self.root)
        self.assertEqual(str(exported[0]['home_team_id']),'3294')
        self.assertEqual(exported[0]['home_team'],'Farul Constanța')
        self.assertEqual(str(exported[0]['sofascore_home_team_id']),'37426')
        self.assertEqual(reep.index(self.root).resolve('team',{'fotmob_id':'210132'})['sofascore_id'],'3294')

    def test_packaging_normalizes_generic_team_fields_without_touching_the_source(self):
        import csv
        from dataset_builder import _atomic_copy,_fixture_team_names
        source=self.root/'source.csv';target=self.root/'copy.csv'
        source.write_text('season,team_id,team,opponent_id,opponent\n2024/25,37426,FC Viitorul Constanța,1,Other\n')
        original=source.read_bytes();_atomic_copy(source,target,'2024-2025',baseline='sofascore')
        with target.open() as f:row=next(csv.DictReader(f))
        self.assertEqual((row['team_id'],row['team']),('3294','Farul Constanța'))
        self.assertEqual(source.read_bytes(),original)
        fixtures=self.root/'fixtures.csv'
        fixtures.write_text('home_team_id,home_team,away_team_id,away_team,fotmob_home_team_id,fotmob_away_team_id\n3294,Farul,1,Other,210132,10\n')
        self.assertEqual(set(_fixture_team_names(fixtures)),{'210132','10'})

    def test_existing_farul_and_viitorul_references_become_one_canonical_row(self):
        import csv
        from fixtures.identityRefresh import reconcile_exports,read_table
        rows=self.rows+[['team','sofascore','3294','rtFarul'],['team','sofascore','37426','rtFarul'],
                        ['team','fotmob','210132','rtFarul'],['team','whoscored','9425','rtFarul']]
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        out=self.root/'exports';path=out/'fixture_concordance/team_ids.csv';path.parent.mkdir(parents=True)
        with path.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['team_name','sofascore_id','fotmob_id','whoscored_id','reep_id','identity_status'])
            writer.writeheader()
            for sid,name in [('3294','FCV Farul Constanța'),('37426','FC Viitorul Constanța')]:
                writer.writerow(dict(team_name=name,sofascore_id=sid,fotmob_id='',whoscored_id='',reep_id='',identity_status='conflict'))
        reconcile_exports(self.root,out)
        _,result=read_table(path)
        self.assertEqual(len(result),1)
        self.assertEqual((result[0]['team_name'],result[0]['sofascore_id'],result[0]['fotmob_id'],result[0]['whoscored_id']),('Farul Constanța','3294','210132','9425'))

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.rows=[['team','sofascore','7703','rtRustavi'],['team','fotmob','321780','rtRustavi'],
                   ['team','fotmob','2222','rtOld'],['team','sofascore','1','rtOther'],['team','fotmob','10','rtOther'],
                   ['player','fotmob','2222','rpPerson']]
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(self.rows),'rows':self.rows})

    def test_reviewed_profiles_resolve_to_one_club_without_rewriting_native_ids(self):
        index=reep.index(self.root)
        self.assertEqual(index.ids('team','fotmob','2222'),{'rtRustavi'})
        self.assertEqual(index.native['team','fotmob','2222'],{'rtOld'})
        self.assertEqual(index.ids('player','fotmob','2222'),{'rpPerson'})
        self.assertEqual(index.resolve('team',{'sofascore_id':'7703'})['fotmob_id'],'321780')
        row=index.resolve('team',{'sofascore_id':'7703','fotmob_id':'2222'})
        self.assertEqual((row['fotmob_id'],row['reep_id'],row['identity_status']),('2222','rtRustavi','linked'))
        self.assertTrue(teams_match({'id':7703,'name':'FC Rustavi'},{'id':2222,'name':'FC Tbilisi'},'fotmob',self.root))
        self.assertFalse(teams_match({'id':999,'name':'Other club'},{'id':2222,'name':'FC Tbilisi'},'fotmob',self.root))

    def test_historical_fixtures_keep_both_native_profiles_under_one_club(self):
        comp=FixtureCompetition('Champions League',7)
        feeds=[(comp,2010,{'sofascore':[event(101,7703,1,'FC Rustavi','Other')],
                          'fotmob':[event(201,2222,10,'FC Tbilisi','Other')]}),
               (comp,2012,{'sofascore':[event(102,7703,1,'FC Rustavi','Other')],
                          'fotmob':[event(202,321780,10,'FC Rustavi','Other')]})]
        rows,*_=concord(feeds,self.root)
        self.assertEqual([r['fotmob_match_id'] for r in rows],['201','202'])
        self.assertEqual([str(r['fotmob_home_team_id']) for r in rows],['2222','321780'])
        self.assertTrue(all(r['reep_home_team_id']=='rtRustavi' for r in rows))
        self.assertTrue(all(r['match_identity_status']!='conflict' for r in rows))

    def test_reviewed_club_revalidates_saved_fixture_without_downloading_profiles(self):
        # This old FotMob match is absent from Reep; the reviewed club IDs,
        # saved season fixture, date and result must establish the match.
        rows=self.rows+[['match','sofascore','101','rm1']]
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        peer=event(201,2222,10,'FC Tbilisi','Other')
        atomic_json(self.root/'_system/fixtures/fotmob/premier-league/2010-2011.json',{'events':[peer]})
        row=dict(sofascore_match_id='101',sofascore_home_team_id='7703',sofascore_away_team_id='1',home_team='FC Rustavi',away_team='Other',
                 competition='Premier League',season='2010-2011',date='2010-09-01',kickoff_utc='2010-09-01T12:00:00+00:00',status='finished',
                 home_score='2',away_score='1',match_identity_status='conflict',fotmob_match_id='',whoscored_match_id='',
                 fotmob_fixture_check='learned_team_mapping_conflict',whoscored_fixture_check='missing')
        repaired,audit=repair_exported_scores([row],self.root,reep.index(self.root))
        self.assertEqual(repaired[0]['fotmob_match_id'],'201')
        self.assertEqual(repaired[0]['fotmob_fixture_check'],'validated')
        self.assertEqual(repaired[0]['match_identity_status'],'unmapped')
        self.assertEqual(audit,[])


if __name__=='__main__':unittest.main()

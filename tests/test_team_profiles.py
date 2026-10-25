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


class ReviewedClubProfiles(unittest.TestCase):
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

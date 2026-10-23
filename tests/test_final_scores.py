"""Period-aware scores must agree without adding shootout kicks or ignoring real conflicts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from providers.scores import final_score
from providers.events import whoscored_fixture
from providers import SofaScoreLoader,WhoScoredLoader
from providers.identity import IdentityError
from tests.test_providers import event,lineup
from dataset_builder import fixtures_from_events
from fixtures.dataset import concord
from fixtures.builder import FixtureCompetition


class FinalScoreTests(unittest.TestCase):
    def test_extra_time_is_the_final_total_not_a_delta_and_penalties_stay_separate(self):
        source=event()
        source['homeScore']={'current':7,'display':1,'normaltime':1,'overtime':2,'penalties':5}
        source['awayScore']={'current':6,'display':1,'normaltime':1,'overtime':2,'penalties':4}
        self.assertEqual((final_score(source,'home'),final_score(source,'away')),(2,2))
        frame=fixtures_from_events([source],'Europa League','2024/2025')
        self.assertEqual((frame.iloc[0].home_score,frame.iloc[0].away_score),(2,2))
        source['homeScore'].pop('overtime');self.assertEqual(final_score(source,'home'),1)
        source['isAwarded']=True;source['homeScore']['display']=3
        self.assertEqual(final_score(source,'home'),3)

    def test_whoscored_schedule_uses_extra_time_and_preserves_zero(self):
        row={'id':1,'startTimeUtc':'2024-08-17T11:30:00Z','homeTeamId':1,'homeTeamName':'Home',
            'awayTeamId':2,'awayTeamName':'Away','homeScore':1,'awayScore':1,
            'homeExtratimeScore':2,'awayExtratimeScore':1,'status':6}
        fixture=whoscored_fixture(row)
        self.assertEqual(final_score(fixture,'home'),2)
        row.update(homeScore=0,homeExtratimeScore=0)
        self.assertEqual(final_score(whoscored_fixture(row),'home'),0)

    def test_enrichment_repairs_old_shootout_score_and_uses_whoscored_extra_time(self):
        source=event();source['homeScore']={'current':7,'normaltime':1,'overtime':2,'penalties':5}
        source['awayScore']={'current':6,'normaltime':1,'overtime':2,'penalties':4}
        sofa=SofaScoreLoader(None);season=sofa.select_season(17,'Premier League',61627,'2024/2025')
        match=sofa._build_match(season,source,lineup())
        self.assertEqual((match.home_score,match.away_score),(2,2))
        match.home_score=7;match.away_score=6  # Checkpoint written by the old loader.
        payload={'match_id':11,'matchCentreEventTypeJson':{},'matchCentreData':{
            'statusCode':6,'startDate':'2024-08-17','events':[{'playerId':77,'teamId':501,'satisfiedEventsTypes':[]}],
            **{side:{'teamId':tid,'name':team,'scores':{'fulltime':1,'extratime':2,'penalty':pen},
                'players':[{'playerId':pid,'name':name,'shirtNo':7,'isFirstEleven':True}],'formations':[]}
                for side,tid,pid,name,team,pen in [('home',501,77,'Player One','Liverpool',5),('away',502,88,'Player Two','Other',4)]}}}
        wrong=deepcopy(payload);wrong['matchCentreData']['home']['scores']['extratime']=3
        with self.assertRaisesRegex(IdentityError,'score mismatch'):WhoScoredLoader().enrich(match,wrong,source)
        self.assertEqual((match.home_score,match.away_score),(7,6))
        WhoScoredLoader().enrich(match,payload,source)
        self.assertEqual((match.home_score,match.away_score),(2,2))

    def test_fixture_concordance_accepts_same_extra_time_total_but_rejects_real_differences(self):
        source=event();source['homeScore']={'current':2,'display':1,'normaltime':1,'overtime':2}
        source['awayScore']={'current':1,'display':1,'normaltime':1,'overtime':1}
        other=deepcopy(source);other['id']=11
        other['homeScore']={'current':2};other['awayScore']={'current':1}
        with tempfile.TemporaryDirectory() as root:
            competition=FixtureCompetition('Premier League',17,fotmob_id=47)
            rows,*_=concord([(competition,2024,{'sofascore':[source],'whoscored':[other]})],Path(root))
            self.assertEqual(rows[0]['whoscored_match_id'],'11');self.assertEqual(rows[0]['home_score'],2)
            other['homeScore']['current']=3
            rows,*_=concord([(competition,2024,{'sofascore':[source],'whoscored':[other]})],Path(root))
            self.assertEqual(rows[0]['whoscored_match_id'],'')

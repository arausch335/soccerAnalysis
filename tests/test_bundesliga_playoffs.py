"""Bundesliga playoff coverage and dataset annotations across provider schedules."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from collection.selection import competition_by_name,competition_phase
from collection.seasonRunner import SeasonRunner
from dataset_builder import fixtures_from_events,package_collection
from fixtures.builder import _fotmob_season_events,COMPETITIONS
from providers.httpClient import ProviderError
from providers.events import fotmob_fixture
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa,Fot,event


class BundesligaPlayoffTests(unittest.TestCase):
    def test_combined_fixture_discovery_includes_both_fotmob_feeds_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows=Fot().season_events(54,'2024/2025')
            client=Mock();client.season_events.side_effect=[rows[:2],rows[1:]]
            runner=SeasonRunner({'sofascore':Sofa(),'fotmob':client},tmp,providers=['sofascore','fotmob'],spatial=(),report=lambda *a,**k:None)
            fixtures,errors=runner._fixtures(competition_by_name('Bundesliga'),'2024/2025',[event()])
            self.assertEqual([call.args[0] for call in client.season_events.call_args_list],[54,9081])
            self.assertEqual(len(fixtures['fotmob']),3);self.assertFalse(errors)
            self.assertEqual(competition_phase('Bundesliga',fixtures['fotmob'][-1]),'playoffs')

    def test_whoscored_includes_both_playoff_legs_with_configured_or_discovered_league_stages(self):
        # Verified WhoScored tournament 388, season 9061, stage 20899.
        for configured in (None,[100]):
            with self.subTest(configured=configured),tempfile.TemporaryDirectory() as tmp:
                competition=competition_by_name('Bundesliga');who=Mock()
                def seasons(path):return [{'name':'2021/2022','path':path+'/seasons/9061'}]
                who.seasons.side_effect=seasons
                who.season_stages.side_effect=lambda path:[20899 if '/388/' in path else 100]
                regular={'id':1,'status':6,'startTimeUtc':'2022-05-14T13:30:00Z',
                    'homeTeamId':47,'homeTeamName':'Hertha Berlin','awayTeamId':38,'awayTeamName':'Hamburger SV'}
                first={**regular,'id':1635702,'startTimeUtc':'2022-05-19T18:30:00Z'}
                second={**regular,'id':1635703,'homeTeamId':38,'homeTeamName':'Hamburger SV',
                    'awayTeamId':47,'awayTeamName':'Hertha Berlin','startTimeUtc':'2022-05-23T18:30:00Z'}
                who.season_events.side_effect=lambda stage,month:[regular] if stage==100 else [first,second]
                runner=SeasonRunner({'sofascore':Sofa(),'whoscored':who},tmp,providers=['sofascore','whoscored'],spatial=(),report=lambda *a,**k:None)
                selected={**event(),'startTimestamp':1652985000}
                with patch('collection.seasonRunner.configured_whoscored_stages',return_value=configured):
                    fixtures,errors=runner._fixtures(competition,'2021/2022',[selected])
                self.assertFalse(errors);self.assertEqual({r['id'] for r in fixtures['whoscored']},{1,1635702,1635703})
                self.assertIn((20899,'202205'),[c.args for c in who.season_events.call_args_list])
                self.assertTrue(any('/tournaments/388/' in c.args[0] for c in who.seasons.call_args_list))
                from fixtures.dataset import _who_events
                from fixtures.builder import FixtureCompetition
                with patch('config.WHOSCORED_STAGE_IDS',{'Bundesliga 2021/2022':configured}):
                    exported=_who_events(who,FixtureCompetition('Bundesliga',35,fotmob_id=54),2021,[selected])
                self.assertEqual({r['id'] for r in exported},{1,1635702,1635703})

    def test_missing_whoscored_playoff_feed_preserves_regular_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            who=Mock()
            who.seasons.side_effect=ProviderError('playoff feed unavailable')
            who.season_events.return_value=[{'id':1,'status':6,'startTimeUtc':'2022-05-14T13:30:00Z',
                'homeTeamId':47,'homeTeamName':'Hertha Berlin','awayTeamId':38,'awayTeamName':'Hamburger SV'}]
            runner=SeasonRunner({'sofascore':Sofa(),'whoscored':who},tmp,providers=['sofascore','whoscored'],spatial=(),report=lambda *a,**k:None)
            with patch('collection.seasonRunner.configured_whoscored_stages',return_value=[100]):
                fixtures,errors=runner._fixtures(competition_by_name('Bundesliga'),'2021/2022',[event()])
            self.assertFalse(errors);self.assertEqual([r['id'] for r in fixtures['whoscored']],[1])

    def test_missing_playoff_feed_does_not_discard_regular_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Mock();client.season_events.side_effect=[Fot().season_events(54,'2024/2025'),ProviderError('feed unavailable')]
            runner=SeasonRunner({'sofascore':Sofa(),'fotmob':client},tmp,providers=['sofascore','fotmob'],spatial=(),report=lambda *a,**k:None)
            fixtures,errors=runner._fixtures(competition_by_name('Bundesliga'),'2024/2025',[event()])
            self.assertEqual(len(fixtures['fotmob']),3);self.assertNotIn('fotmob',errors)

    def test_fixture_exports_keep_playoffs_inside_bundesliga_with_note_and_native_round(self):
        regular=event(1);regular['tournament']['name']='Bundesliga'
        playoff=deepcopy(regular);playoff['id']=2
        playoff['tournament'].update(name='Bundesliga, Relegation/Promotion Playoffs',qualificationOrPreliminary=True)
        playoff['roundInfo']={'name':'Final','round':29}
        frame=fixtures_from_events([regular,playoff],'Bundesliga','2024/2025')
        self.assertEqual(frame.match_id.tolist(),[1,2])
        self.assertEqual(frame.competition.tolist(),['Bundesliga','Bundesliga'])
        self.assertEqual(frame.competition_phase.tolist(),['main','playoffs'])
        self.assertEqual(frame.iloc[1].round_name,'Final');self.assertIn('9081',frame.iloc[1].competition_note)
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'data/bundesliga/2024-2025'
            atomic_json(source/'config.json',{'competition':'Bundesliga','season':'2024/2025'})
            atomic_json(source/'schedule.json',{'events':[regular,playoff]})
            output=package_collection(source,Path(tmp)/'dataset',tables=['fixtures'])
            self.assertIn('playoffs',(output/'fixtures.csv').read_text())

    def test_fotmob_fixture_backfill_labels_separate_playoff_feed(self):
        competition=next(c for c in COMPETITIONS if c.name=='Bundesliga')
        client=Mock();client.seasons.return_value=['2024/2025']
        rows=Fot().season_events(54,'2024/2025');client.season_events.side_effect=[rows[:1],rows[1:]]
        events=_fotmob_season_events(client,competition,2024,include_upcoming=False)
        frame=fixtures_from_events(events,'Bundesliga','2024/2025')
        self.assertEqual(frame.competition_phase.tolist(),['main','playoffs','playoffs'])
        self.assertEqual(competition_phase('Bundesliga',fotmob_fixture(rows[1],competition_id=9081)),'playoffs')

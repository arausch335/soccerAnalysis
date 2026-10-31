"""Domestic cups share the fixture pipeline, with safe edition and replay rules."""
import csv
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from fixtures.builder import COMPETITIONS,CUP_COMPETITIONS,FixtureCompetition
from fixtures.dataset import build,_who_paths,_who_events,_fotmob_events,schedules
from fixtures.reconcile import reconcile
from fixtures.seasons import select,in_window,years
from providers.sofascore_ids import SOFASCORE_COMPETITION_IDS
from providers.whoScoredClient import _Directory
from tests.test_fixture_dataset import Sofa,Fot,Who,event,NOW


def cup(name):return next(c for c in CUP_COMPETITIONS if c.name==name)


class FixtureCupTests(unittest.TestCase):
    def test_all_twelve_cups_are_in_default_scope_and_retired_cup_stops_in_2019(self):
        self.assertEqual(len(CUP_COMPETITIONS),12)
        self.assertEqual(sum(c.kind=='supercup' for c in CUP_COMPETITIONS),5)
        self.assertTrue(set(CUP_COMPETITIONS)<=set(COMPETITIONS))
        self.assertEqual(list(years(cup('Coupe de la Ligue'),2010,2026)),list(range(2010,2020)))
        self.assertEqual(list(years(cup('Coupe de la Ligue'),2020,2026)),[])
        for c in CUP_COMPETITIONS:
            self.assertEqual(c.sofascore_id,SOFASCORE_COMPETITION_IDS[c.name])
            self.assertTrue(c.fotmob_id and c.country and c.whoscored_parent)

    def test_calendar_years_are_combined_by_playing_window_not_guessed(self):
        comp=cup('Supercoppa Italiana')
        chosen,window=select(comp,2024,['2023','2024','2025','2026'])
        self.assertEqual(chosen,['2024','2025']);self.assertTrue(window)
        def stamped(date):return event(day=int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()))
        self.assertFalse(in_window(stamped('2024-01-06'),2024))
        self.assertTrue(in_window(stamped('2024-08-01'),2024))
        self.assertTrue(in_window(stamped('2025-01-06'),2024))
        self.assertFalse(in_window(stamped('2025-08-01'),2024))
        self.assertEqual(select(comp,2024,['2024','2025','2024/2025']),(['2024/2025'],False))
        self.assertEqual(select(comp,2024,['24/25','25/26']),(['24/25'],False))
        self.assertEqual(select(cup('FA Cup'),2010,['09/10','10/11']),(['10/11'],False))
        with self.assertRaisesRegex(ValueError,'Ambiguous'):select(comp,2024,['2024','2024'])

    def test_normal_cup_paired_season_keeps_a_delayed_final(self):
        comp=cup('Copa del Rey')
        chosen,window=select(comp,2019,['2018/2019','2019/2020','2020/2021','unknown'])
        self.assertEqual(chosen,['2019/2020']);self.assertFalse(window)
        client=Fot();client.seasons=lambda cid:['2019/2020']
        client.season_events=lambda cid,season:[dict(id=500,home=dict(id=10,name='Home'),away=dict(id=20,name='Away'),
            status=dict(utcTime='2021-04-03T19:30:00Z',finished=True,scoreStr='1 - 0'))]
        events,errors=_fotmob_events(client,comp,2019)
        self.assertFalse(errors);self.assertEqual(events[0]['id'],500)

    def test_each_cup_discovers_published_country_route_without_invented_ids(self):
        for comp in CUP_COMPETITIONS:
            with self.subTest(competition=comp.name):
                # Include a misleading women's route; exact slug matching rejects it.
                slug=comp.whoscored_suffixes[0]
                route=f'regions/1/tournaments/900/{slug}'
                parser=_Directory();parser.feed(f'<a href="/{route}">Cup</a><a href="/regions/1/tournaments/901/womens-{slug}">Women</a>')
                client=Mock();client._directory.side_effect=lambda path,ready:parser
                self.assertEqual(_who_paths(client,comp),[route])

    def test_retired_cup_can_be_found_in_historical_parent_menu(self):
        comp=cup('Coupe de la Ligue');client=Mock()
        empty=_Directory();found=_Directory()
        route='regions/74/tournaments/93/france-coupe-de-la-ligue'
        found.feed(f'<a href="/{route}">Coupe de la Ligue</a>')
        client._directory.side_effect=lambda path,ready: found if path=='historical-parent' else empty
        client.seasons.return_value=[dict(name='2019/2020',path='historical-parent')]
        self.assertEqual(_who_paths(client,comp,2019),[route])

    def test_who_single_year_seasons_keep_january_fixture_in_correct_paired_season(self):
        comp=cup('Supercoppa Italiana');client=Who()
        client.seasons=lambda path:[dict(name='2024',path='s2024'),dict(name='2025',path='s2025')]
        client.season_stages=lambda path:[int(path[1:])]
        def matches(stage,month):
            date='2024-01-01' if stage==2024 else '2025-01-06'
            return [dict(id=stage,startTimeUtc=date+'T12:00:00Z',homeTeamId=30,homeTeamName='Home',
                         awayTeamId=40,awayTeamName='Away',homeScore=2,awayScore=1,status=6)]
        client.season_events=matches
        with patch('fixtures.dataset._who_paths',return_value=['published-cup']):
            events=_who_events(client,comp,2024,[])
        self.assertEqual([e['id'] for e in events],[2025])

    def test_sofa_and_fotmob_single_year_schedules_use_same_july_june_window(self):
        comp=cup('Supercoppa Italiana');sofa=Sofa();fot=Fot();who=Who()
        sofa.seasons=lambda cid:{'seasons':[dict(id=24,year='2024'),dict(id=25,year='2025')]}
        def sofa_events(cid,sid,**kw):
            date='2024-01-01' if sid==24 else '2025-01-06'
            yield event(mid=sid,day=int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()))
        sofa.iter_season_events=sofa_events
        fot.seasons=lambda cid:['2024','2025']
        fot.season_events=lambda cid,season:[dict(id=int(season),home=dict(id=10,name='Home'),away=dict(id=20,name='Away'),
            status=dict(utcTime=('2024-01-01' if season=='2024' else '2025-01-06')+'T12:00:00Z',finished=True,scoreStr='2 - 1'))]
        who.seasons=lambda path:[dict(name='2024/2025',path='s2024')]
        with tempfile.TemporaryDirectory() as tmp,patch('fixtures.dataset._who_paths',return_value=['published-cup']):
            feeds,records=schedules(Path(tmp),dict(sofascore=sofa,fotmob=fot,whoscored=who),comp,2024)
        self.assertEqual([e['id'] for e in feeds['sofascore']],[25])
        self.assertEqual([e['id'] for e in feeds['fotmob']],[2025])

    def test_cup_fixture_uses_shared_team_ids_and_export_scores_without_league_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';output=Path(tmp)/'dataset/fixtures.csv'
            options=dict(data_root=root,output_csv=output,clients=dict(sofascore=Sofa(),fotmob=Fot(),whoscored=Who()),
                start_season=2010,end_season=2010,now=NOW,send_to_main=False,live_reports=False,report=lambda *a,**k:None)
            with patch('fixtures.dataset._who_paths',return_value=['published-cup']):
                first=build(competitions=(FixtureCompetition('Premier League',17,fotmob_id=47),),**options)
                with output.open() as f:league_row=next(csv.DictReader(f))
                result=build(competitions=(cup('FA Cup'),),**options)
            with output.open() as f:row=next(csv.DictReader(f))
            self.assertEqual(row['home_entity_id'],league_row['home_entity_id'])
            self.assertTrue(row['entity_id'].startswith('am'))
            self.assertEqual([row[p+'_match_id'] for p in ('sofascore','fotmob','whoscored')],['100','200','300'])
            self.assertEqual((row['competition_type'],row['competition_country']),('cup','England'))
            self.assertEqual((row['season'],row['home_score'],row['away_score']),('2010-2011','2','1'))
            self.assertTrue(result['required_schedule_coverage_complete']);self.assertTrue(result['count_checks_passed'])
            self.assertFalse(first['count_checks_passed'])

    def test_upcoming_cup_replay_and_both_legs_are_not_collapsed_into_played_game(self):
        first={**event(),'roundInfo':{'name':'Semi-finals'}}
        replay={**event(101,day=first['startTimestamp']+7*86400),'status':{'type':'notstarted'},'roundInfo':{'name':'Semi-finals'}}
        retained,audit=reconcile([first,replay],'sofascore','FA Cup',2010)
        self.assertEqual(len(retained),2);self.assertFalse(audit)
        second={**event(102,home=2,away=1,day=first['startTimestamp']+14*86400),'roundInfo':{'name':'Semi-finals','leg':2}}
        retained,audit=reconcile([first,second],'sofascore','Coppa Italia',2010)
        self.assertEqual(len(retained),2);self.assertFalse(audit)


if __name__=='__main__':unittest.main()

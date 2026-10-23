"""Historical fixture discovery and conservative cross-provider concordance."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock

from fixtures.builder import COMPETITIONS, FixtureCompetition
from fixtures.dataset import (COLUMNS, ID_COLUMNS, _fotmob_events, _who_paths,
                              build, concord, schedules)
from providers import reep
from providers.httpClient import ProviderRequestError
from providers.sofaScoreClient import SofaScoreClient
from providers.whoScoredClient import WhoScoredClient
from storage.pandasStore import atomic_json


DAY = 1283342400  # 2010-09-01 UTC
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def event(mid=100, home=1, away=2, home_name='Home', away_name='Away', day=DAY):
    return dict(id=mid, startTimestamp=day, homeTeam=dict(id=home, name=home_name),
                awayTeam=dict(id=away, name=away_name), homeScore=dict(current=2),
                awayScore=dict(current=1), status=dict(type='finished'))


class Sofa:
    refresh_cache = False

    def __init__(self):
        self.calls = []

    def seasons(self, cid):
        self.calls.append(('seasons', cid))
        return {'seasons': [{'id': 10, 'year': '2010/2011'}, {'id': 11, 'year': '2011/2012'}]}

    def iter_season_events(self, cid, sid, *, include_upcoming):
        self.calls.append(('events', cid, sid, include_upcoming))
        yield {**event(sid * 10), 'season': {'id': sid}, 'tournament': {'uniqueTournament': {'id': cid}}}


class Fot:
    refresh_cache = False

    def seasons(self, cid):
        return ['2010/2011', '2011/2012']

    def season_events(self, cid, season):
        return [dict(id=200 + (int(season[:4]) - 2010) * 10,
            home=dict(id=10, name='Home FC'), away=dict(id=20, name='Away FC'),
            status=dict(utcTime='2010-09-01T12:00:00Z', finished=True, scoreStr='2 - 1'))]


class Who:
    refresh_cache = False

    def seasons(self, path):
        return [{'name': '2010/2011', 'path': 's2010'}, {'name': '2011/2012', 'path': 's2011'}]

    def season_stages(self, path):
        return [int(path[1:])]

    def season_events(self, stage, month):
        return [dict(id=300 + (stage - 2010) * 10, startTimeUtc='2010-09-01T12:00:00Z',
            homeTeamId=30, homeTeamName='Home', awayTeamId=40, awayTeamName='Away',
            homeScore=2, awayScore=1, status=6)]


class FixtureDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'data'
        self.output = self.base / 'dataset' / 'fixtures.csv'
        self.competition = FixtureCompetition('Premier League', 17, fotmob_id=47)
        self.clients = dict(sofascore=Sofa(), fotmob=Fot(), whoscored=Who())
        rows = [
            ['team', 'sofascore', '1', 'rt1'], ['team', 'fotmob', '10', 'rt1'], ['team', 'whoscored', '30', 'rt1'],
            ['team', 'sofascore', '2', 'rt2'], ['team', 'fotmob', '20', 'rt2'], ['team', 'whoscored', '40', 'rt2'],
            ['match', 'sofascore', '100', 'rm1'], ['match', 'fotmob', '200', 'rm1'], ['match', 'whoscored', '300', 'rm1'],
        ]
        reep.install(self.root, dict(format=reep.FORMAT, version=reep._digest(rows), rows=rows, release='test'))

    def build(self, **kwargs):
        return build(data_root=self.root, output_csv=self.output, clients=self.clients,
            competitions=(self.competition,), start_season=2010, end_season=2010,
            now=NOW, report=lambda *a, **k: None, **kwargs)

    def records(self):
        with self.output.open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(tuple(reader.fieldnames), COLUMNS)
            return list(reader)

    def test_four_ids_per_entity_and_scores_season_names(self):
        manifest = self.build()
        row, = self.records()
        self.assertEqual(len(ID_COLUMNS), 12)
        self.assertTrue(all(row[c] for c in ID_COLUMNS))
        self.assertEqual([row[p + '_match_id'] for p in ('sofascore', 'fotmob', 'whoscored', 'reep')], ['100', '200', '300', 'rm1'])
        self.assertEqual([row[p + '_home_team_id'] for p in ('sofascore', 'fotmob', 'whoscored', 'reep')], ['1', '10', '30', 'rt1'])
        self.assertEqual((row['season'], row['competition'], row['home_score'], row['away_score']), ('2010-2011', 'Premier League', '2', '1'))
        self.assertEqual((row['home_team'], row['fotmob_home_team']), ('Home', 'Home FC'))
        self.assertEqual(row['fotmob_fixture_check'], 'validated')
        self.assertTrue(manifest['schedule_coverage_complete'])

    def test_offline_resume_does_not_call_any_provider(self):
        self.build()
        with patch.object(self.clients['sofascore'], 'seasons', side_effect=AssertionError('Network not allowed')):
            manifest = self.build(offline=True)
        self.assertEqual(manifest['rows'], 1)

    def test_output_filter_is_independent_of_checkpoint(self):
        self.clients['sofascore'].iter_season_events = lambda *a, **k: iter([
            {**event(), 'status': {'type': 'notstarted'}}])
        self.build(include_upcoming=False)
        self.assertEqual(self.records(), [])
        self.build(offline=True, include_upcoming=True)
        self.assertEqual(len(self.records()), 1)

    def test_typed_reep_ids_are_retained_on_next_refresh(self):
        self.build()
        wanted = reep.wanted_ids(self.root, self.output.parent)
        self.assertIn(('match', 'sofascore', '100'), wanted)
        self.assertIn(('match', 'fotmob', '200'), wanted)
        self.assertIn(('team', 'whoscored', '40'), wanted)

    def test_wrong_score_day_or_sides_rejects_even_reep_match_id(self):
        base = event()
        other = event(200, 10, 20)
        for bad in ({**other, 'homeScore': {'current': 5}},
                    {**other, 'startTimestamp': DAY + 86400},
                    {**other, 'homeTeam': other['awayTeam'], 'awayTeam': other['homeTeam']}):
            with self.subTest(bad=bad):
                rows, _, _, _, issues = concord([(self.competition, 2010,
                    {'sofascore': [base], 'fotmob': [bad]})], self.root)
                self.assertEqual(rows[0]['fotmob_match_id'], '')
                self.assertEqual(rows[0]['match_identity_status'], 'conflict')
                self.assertTrue(any(i['issue'] == 'reep_fixture_failed_validation' for i in issues))

    def test_names_without_reep_can_learn_ids_across_seasons(self):
        root = self.base / 'no_reep'
        early = event(101, home_name='Old name', away_name='Other old name')
        late = event(102, day=DAY + 86400)
        feeds = [(self.competition, 2010, {'sofascore': [early], 'fotmob': [event(201, 10, 20, 'H', 'A')]}),
                 (self.competition, 2011, {'sofascore': [late], 'fotmob': [event(202, 10, 20, day=DAY + 86400)]})]
        rows, teams, _, aliases, _ = concord(feeds, root)
        self.assertEqual(rows[0]['fotmob_match_id'], '201')
        self.assertEqual(rows[0]['fotmob_home_team_id'], '10')
        self.assertEqual(rows[0]['home_team'], 'Old name')
        self.assertTrue(any(a['method'] == 'learned_team_ids_and_fixture' for a in aliases))

    def test_ambiguous_fixture_and_team_collisions_are_never_guessed(self):
        root = self.base / 'no_reep'
        feeds = [(self.competition, 2010, {'sofascore': [event()],
            'fotmob': [event(200, 10, 20), event(201, 10, 20)]})]
        rows, _, _, _, issues = concord(feeds, root)
        self.assertEqual(rows[0]['fotmob_match_id'], '')
        self.assertTrue(any(i['issue'] == 'ambiguous_fixture' for i in issues))
        feeds = [(self.competition, 2010, {'sofascore': [event(), event(101, day=DAY + 86400)],
            'fotmob': [event(200, 10, 20), event(201, 11, 20, day=DAY + 86400)]})]
        rows, _, _, _, issues = concord(feeds, root)
        self.assertTrue(all(r['fotmob_home_team_id'] == '' for r in rows))
        self.assertTrue(all(r['home_team_identity_status'] == 'conflict' for r in rows))

    def test_qualifiers_and_conference_start_year(self):
        self.assertEqual(next(c for c in COMPETITIONS if c.name == 'Conference League').first_season, 2021)
        qualifying = {**event(), 'tournament': {'qualificationOrPreliminary': True}}
        rows, *_ = concord([(FixtureCompetition('Champions League', 7), 2010,
                            {'sofascore': [qualifying]})], self.root)
        self.assertEqual(rows[0]['competition'], 'Champions League Qualification')
        self.assertEqual(rows[0]['competition_phase'], 'qualifying')

    def test_opponent_day_and_result_establish_unknown_team_name_link(self):
        root = self.base / 'no_reep'
        feeds = [(self.competition, 2010, {'sofascore': [event(home_name='Baseline club')],
            'fotmob': [event(200, 10, 20, home_name='Provider nickname')]})]
        rows, teams, _, aliases, _ = concord(feeds, root)
        self.assertEqual(rows[0]['fotmob_match_id'], '200')
        self.assertEqual(rows[0]['fotmob_home_team_id'], '10')
        self.assertEqual(rows[0]['fotmob_match_method'], 'opponent_date_score')
        self.assertTrue(any(a['method'] == 'opponent_date_score' for a in aliases))
        feeds[0][2]['fotmob'][0]['awayTeam']['name'] = 'Unknown opponent'
        rows, *_ = concord(feeds, root)
        self.assertEqual(rows[0]['fotmob_match_id'], '')

    def test_unknown_team_context_requires_a_unique_result_and_known_opponent(self):
        root = self.base / 'no_reep'
        feeds = [(self.competition, 2010, {'sofascore': [event(home_name='Baseline club')],
            'fotmob': [event(200, 10, 20, home_name='Nickname'), event(201, 11, 20, home_name='Another name')]})]
        rows, _, _, _, issues = concord(feeds, root)
        self.assertEqual(rows[0]['fotmob_match_id'], '')
        self.assertTrue(any(i['issue'] == 'ambiguous_fixture' for i in issues))

    def test_same_result_in_another_competition_is_not_a_candidate(self):
        root = self.base / 'no_reep'
        rows, *_ = concord([
            (self.competition, 2010, {'sofascore': [event()], 'fotmob': []}),
            (FixtureCompetition('Championship', 18), 2010, {'sofascore': [event(101)], 'fotmob': [event(201, 10, 20)]}),
        ], root)
        self.assertEqual(rows[0]['fotmob_match_id'], '')
        self.assertEqual(rows[1]['fotmob_match_id'], '201')

    def test_wrong_source_competition_is_not_exported_as_partial_baseline(self):
        self.clients['sofascore'].iter_season_events = lambda *a, **k: iter([
            {**event(), 'tournament': {'uniqueTournament': {'id': 999}}}])
        with self.assertRaisesRegex(ValueError, 'No Sofascore schedules'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_missing_supplement_retains_main_schedule_as_partial(self):
        competition = FixtureCompetition('Champions League', 7, fotmob_id=42, fotmob_qualification_id=10611)
        client = Fot()
        client.seasons = lambda cid: ['2010/2011'] if cid == 42 else []
        events, errors = _fotmob_events(client, competition, 2010)
        self.assertEqual(len(events), 1)
        self.assertTrue(errors)

    def test_failing_who_qualification_discovery_keeps_main_fixture_ids(self):
        competition = FixtureCompetition('Champions League', 7, fotmob_id=42)
        self.clients['whoscored'].qualification_path = lambda path: (_ for _ in ()).throw(ValueError('No qualifier directory'))
        feeds, coverage = schedules(self.root, self.clients, competition, 2010)
        self.assertEqual(feeds['whoscored'][0]['id'], 300)
        who, = [c for c in coverage if c['provider'] == 'whoscored']
        self.assertEqual(who['status'], 'partial')

    def test_incomplete_baseline_pagination_retains_valid_rows_as_partial(self):
        def pages(*args, **kwargs):
            yield event()
            raise ValueError('Second page failed')
        self.clients['sofascore'].iter_season_events = pages
        manifest = self.build()
        self.assertEqual(len(self.records()), 1)
        self.assertFalse(manifest['schedule_coverage_complete'])

    def test_empty_upcoming_404_saves_complete_schedule_and_reuses_it(self):
        client = self.clients['sofascore']
        client.iter_season_events = SofaScoreClient.iter_season_events.__get__(client)
        def page(cid, sid, index, direction):
            if direction == 'next':
                raise ProviderRequestError('HTTP 404 for events/next/0', status_code=404)
            return {'events': [event()], 'hasNextPage': False}
        client.season_events = Mock(side_effect=page)
        manifest = self.build()
        self.assertTrue(manifest['schedule_coverage_complete'])
        self.assertEqual(manifest['rows'], 1)
        self.assertEqual(client.season_events.call_count, 2)
        with patch.object(client, 'season_events', side_effect=AssertionError('Checkpoint should be reused')):
            resumed = self.build(offline=True)
        self.assertTrue(resumed['schedule_coverage_complete'])
        self.assertEqual(resumed['rows'], 1)

    def test_failed_refresh_retains_checkpoint_and_reports_gap(self):
        self.build()
        self.clients['sofascore'].seasons = lambda cid: (_ for _ in ()).throw(ValueError('unavailable'))
        self.build(refresh_schedules=True)
        self.assertEqual(len(self.records()), 1)
        with (self.output.parent / 'fixture_concordance/coverage.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        sofa, = [r for r in rows if r['provider'] == 'sofascore']
        self.assertEqual((sofa['status'], sofa['source']), ('partial', 'stale_checkpoint'))

    def test_no_baseline_does_not_replace_existing_dataset(self):
        self.output.parent.mkdir()
        self.output.write_text('existing\n')
        with self.assertRaisesRegex(ValueError, 'preserved'):
            self.build(offline=True)
        self.assertEqual(self.output.read_text(), 'existing\n')

    def test_authentication_failure_pauses_provider_for_remaining_seasons(self):
        self.clients['sofascore'].seasons = lambda cid: (_ for _ in ()).throw(ProviderRequestError('Forbidden', status_code=403))
        blocked = {}
        _, first = schedules(self.root, self.clients, self.competition, 2010, blocked=blocked)
        self.assertIn('sofascore', blocked)
        with patch.object(self.clients['sofascore'], 'seasons', side_effect=AssertionError('Repeated blocked request')):
            feeds, second = schedules(self.root, self.clients, self.competition, 2011, blocked=blocked)
        self.assertFalse(feeds)
        self.assertEqual(len(second), 3)

    def test_directory_discovery_and_conference_qualifiers_use_published_paths(self):
        client = Who()
        client.get_text = lambda path: '<a href="/Regions/206/Tournaments/777/spain-segunda-division">LaLiga 2</a>'
        competition = next(c for c in COMPETITIONS if c.name == 'La Liga 2')
        self.assertEqual(_who_paths(client, competition), ['regions/206/tournaments/777/spain-segunda-division'])
        who = WhoScoredClient()
        who.get_text = lambda path: ('<a href="/regions/250/tournaments/999/europe-conference-league-qualification">Q</a>'
                                    '<a href="/regions/250/tournaments/998/europe-europa-league-qualification">Other</a>')
        self.assertEqual(who.qualification_path('europe-conference-league'), 'regions/250/tournaments/999/europe-conference-league-qualification')

    def test_second_division_discovery_uses_published_country_tournament_dropdown(self):
        client=Who()
        client.get_text=Mock(return_value='''<select id="tournaments">
            <option value="/Regions/252/Tournaments/7/england-championship">Championship</option></select>
            <a href="/regions/252/tournaments/7/england-championship">Championship</a>
            <a href="/regions/252/tournaments/7/seasons/11184/stages/25588/show/england-championship-2026-2027">Current season</a>''')
        competition=next(c for c in COMPETITIONS if c.name=='Championship')
        self.assertEqual(_who_paths(client,competition),['regions/252/tournaments/7/england-championship'])
        client.get_text.assert_called_once_with('regions/252/tournaments/2/england-premier-league')

    def test_fixture_transports_share_one_persistent_extension_connection(self):
        from contextlib import ExitStack
        from providers.transports.bridge import acquire_bridge
        from providers.transports.sofascore import SofaScoreTransport
        from providers.transports.fotmob import FotMobTransport
        from providers.transports.whoscored import WhoScoredTransport
        shared=Mock();bindings=[]
        # Use the actual transport constructors. Only replace network discovery
        # with the deterministic schedule fixtures used throughout this suite.
        def factory(provider,transport):
            def create(root,**kwargs):
                client=transport(root,**kwargs)
                bridge,owns=acquire_bridge()
                bindings.append((provider,bridge,owns))
                stub=self.clients[provider]
                for name in ('seasons','iter_season_events','season_events','season_stages'):
                    if hasattr(stub,name):setattr(client,name,getattr(stub,name))
                return client
            return create
        with ExitStack() as stack:
            made=stack.enter_context(patch('providers.transports.bridge.ExtensionBridge',return_value=shared))
            for provider,transport in [('sofascore',SofaScoreTransport),('fotmob',FotMobTransport),('whoscored',WhoScoredTransport)]:
                module={'sofascore':'sofascore','fotmob':'fotmob','whoscored':'whoscored'}[provider]
                stack.enter_context(patch('providers.transports.'+module+'.'+transport.__name__,side_effect=factory(provider,transport)))
            manifest=build(data_root=self.root,output_csv=self.output,competitions=(self.competition,),
                start_season=2010,end_season=2010,now=NOW,report=lambda *a,**k:None,send_to_main=False)
        self.assertEqual(manifest['rows'],1)
        self.assertEqual(len(bindings),3)
        self.assertTrue(all(bridge is shared and not owns for _,bridge,owns in bindings))
        made.assert_called_once();shared.start.assert_called_once();shared.close.assert_called_once()
        self.assertEqual(made.call_args.kwargs['session_path'],self.root/'_system/config/extension-session.json')
        self.assertEqual(made.call_args.kwargs['diagnostics_dir'],self.root/'_system/extension_diagnostics/fixtures')

    def test_offline_fixture_build_does_not_start_an_extension_connection(self):
        self.build()
        with patch('providers.transports.bridge.ExtensionBridge') as made:
            # The saved schedules are sufficient even when default transports
            # are constructed. No extension connection is opened offline.
            result=build(data_root=self.root,output_csv=self.output,competitions=(self.competition,),
                start_season=2010,end_season=2010,now=NOW,offline=True,send_to_main=False,report=lambda *a,**k:None)
        self.assertEqual(result['rows'],1);made.return_value.start.assert_not_called()

    def test_main_fixture_route_bypasses_worker_startup(self):
        import main
        with patch('fixtures.dataset.main', return_value=0) as run:
            self.assertEqual(main.entrypoint(['--fixtures', '--offline']), 0)
        run.assert_called_once_with(['--offline'])


if __name__ == '__main__':
    unittest.main()

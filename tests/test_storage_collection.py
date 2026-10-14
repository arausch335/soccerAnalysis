from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from providers import SofaScoreLoader, SofaScoreRequestError
from collection import CollectionConfig, CollectionRunner
from models import FootballDataset, Player, Competition, derive_traits
from storage import PandasStore


def event(i, team_id=44):
    return {'id': i, 'startTimestamp': 1723894200 + i * 86400,
            'season': {'id': 61627}, 'tournament': {'uniqueTournament': {'id': 17}},
            'status': {'type': 'finished'},
            'homeTeam': {'id': team_id, 'name': 'Liverpool'},
            'awayTeam': {'id': 2, 'name': 'NA'},
            'homeScore': {'current': 0}, 'awayScore': {'current': 2}}


class Client:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.incomplete = set()

    def lineups(self, match_id):
        self.calls.append(match_id)
        if match_id in self.fail:
            raise SofaScoreRequestError('temporary failure', status_code=503)
        if match_id in self.incomplete:
            return {'home': {'players': []}}
        return {'confirmed': True, 'home': {'formation': '4-3-3', 'players': [
            {'player': {'id': 7, 'name': 'Salah, "Mo"\nمحمد', 'position': 'F'},
             'substitute': False, 'shirtNumber': 11, 'statistics': {'minutesPlayed': 90,
                 'totalPass': 30, 'accuratePass': 24, 'zero': 0, 'absent': None,
                 'nested': {'data': [True, False, 'NA', '']}}},
            {'player': {'id': 8, 'name': '', 'position': 'M'}, 'substitute': True}]},
                'away': {'players': [{'player': {'id': 9, 'name': 'Defender', 'position': 'D'},
                                      'substitute': False, 'statistics': {'minutesPlayed': 90}}]}}


class StorageTests(unittest.TestCase):
    def dataset(self):
        loader = SofaScoreLoader(Client())
        season = loader.select_season(17, 'Premier League', 61627, '24/25')
        matches = list(loader.iter_matches(season, [event(1), event(2)]))
        p = loader.dataset.players[7]
        p.information = {'height': None, 'age': 32}
        p.performances[1].stats.record_action('passing', 30, 24)
        p.performances[2].stats.record_action('passing', None, 0)
        derive_traits(p, matches[1])
        reserve = Player(100, 'Reserve')
        loader.dataset.add_player(reserve)
        loader.dataset.teams[44].players[100] = reserve
        cup = Competition(99, 'Cup')
        loader.dataset.add_competition(cup)
        loader.dataset.teams[44].competitions[99] = cup
        return loader.dataset

    def check_restored(self, ds):
        m, second = ds.matches[1], ds.matches[2]
        p = ds.players[7]
        self.assertIs(m.home_team, second.home_team)
        self.assertIs(m.home_team, ds.teams[44])
        self.assertIs(m.season, ds.competitions[17].seasons[61627])
        self.assertIs(p, m.home_lineup.players[0].player)
        self.assertIs(p.performances[1].lineup, m.home_lineup)
        self.assertEqual(p.performances[1].stats.actions['passing'].rate, .8)
        self.assertIsNone(p.performances[2].stats.actions['passing'].attempts)
        self.assertEqual(p.traits[2].passing.values['completion_rate'], .8)
        self.assertEqual(p.traits[2].source_match_ids, [1])
        self.assertIs(p.traits[2].before_match, second)
        self.assertIsNone(ds.players[8].performances[1].minutes_played)
        self.assertFalse(ds.players[8].performances[1].starter)
        self.assertIsNone(m.round_number)
        self.assertEqual(m.home_score, 0)
        self.assertEqual(ds.teams[2].name, 'NA')
        self.assertEqual(ds.players[8].name, '')
        self.assertEqual(p.name, 'Salah, "Mo"\nمحمد')
        self.assertIs(ds.teams[44].players[100], ds.players[100])
        self.assertIs(ds.teams[44].competitions[99], ds.competitions[99])
        self.assertEqual(p.performances[1].stats.raw['nested']['data'], [True, False, 'NA', ''])

    def test_dataframe_roundtrip_and_analysis_view(self):
        original = self.dataset()
        frames = PandasStore.to_frames(original)
        restored = PandasStore.from_frames(frames)
        self.check_restored(restored)
        table = PandasStore.player_stats_frame(restored)
        self.assertEqual(table.loc[table.player_id.eq(7), 'stat.accuratePass'].tolist(), [24, 24])
        restored.players[7].information['age'] = 99
        self.assertEqual(original.players[7].information['age'], 32)

    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PandasStore(folder)
            store.to_csv(self.dataset())
            restored = store.from_csv()
            self.check_restored(restored)
            self.assertEqual(len(list(store.current_directory.glob('*.csv'))), 11)

    def test_empty_dataset_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PandasStore(folder)
            store.to_csv(FootballDataset())
            self.assertEqual(store.from_csv().matches, {})

    def test_interrupted_checkpoint_keeps_previous_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PandasStore(folder)
            ds = self.dataset()
            old = store.to_csv(ds)
            ds.players[7].name = 'Changed'
            with patch('storage.pandasStore.atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): store.to_csv(ds)
            self.assertEqual(store.current_directory, old)
            self.assertNotEqual(store.from_csv().players[7].name, 'Changed')

    def test_successful_commit_prunes_superseded_generations(self):
        with tempfile.TemporaryDirectory() as folder:
            store=PandasStore(folder);first=store.to_csv(self.dataset())
            second=store.to_csv(self.dataset())
            self.assertFalse(first.exists());self.assertTrue(second.exists())
            self.assertEqual([path for path in Path(folder,'snapshots').iterdir() if path.is_dir()],[second])

    def test_duplicate_keys_and_broken_references_are_rejected(self):
        frames = PandasStore.to_frames(self.dataset())
        frames['teams'] = pd.concat([frames['teams'], frames['teams'].iloc[:1]])
        with self.assertRaisesRegex(ValueError, 'Duplicate keys'):
            PandasStore.from_frames(frames)
        frames = PandasStore.to_frames(self.dataset())
        frames['matches'].loc[0, 'home_team_id'] = 999
        with self.assertRaisesRegex(ValueError, 'Broken table reference'):
            PandasStore.from_frames(frames)


class RunnerTests(unittest.TestCase):
    def test_first_ten_finished_matches_resume_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            runner = CollectionRunner(client, folder, report=lambda _: None)
            excluded = event(0); excluded['status']['type'] = 'notstarted'
            events = [event(i) for i in range(12, 0, -1)] + [excluded, event(999, 9), event(1)]
            summary = runner.run(events)
            self.assertEqual(client.calls, list(range(1, 11)))
            self.assertTrue(summary.status.eq('complete').all())
            self.assertEqual(len(runner.dataset.matches), 10)
            self.assertEqual(len(runner.dataset.players[7].performances), 10)
            new = CollectionRunner(client, folder, report=lambda _: None)
            new.run()
            self.assertEqual(client.calls, list(range(1, 11)))
            self.assertEqual(len(new.dataset.matches), 10)

    def test_failed_match_is_retried_without_replacing_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(); client.fail.add(1)
            config = CollectionConfig(limit=2)
            runner = CollectionRunner(client, folder, config, report=lambda _: None)
            summary = runner.run([event(1), event(2), event(3)])
            self.assertEqual(summary.status.tolist(), ['failed', 'complete'])
            self.assertEqual(list(runner.dataset.matches), [2])
            client.fail.clear()
            retry = CollectionRunner(client, folder, config, report=lambda _: None)
            summary = retry.run()
            self.assertEqual(client.calls, [1, 2, 1])
            self.assertTrue(summary.status.eq('complete').all())
            self.assertEqual(summary.attempts.tolist(), [2, 1])

    def test_raw_response_survives_storage_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(); config = CollectionConfig(limit=1)
            runner = CollectionRunner(client, folder, config, report=lambda _: None)
            with patch.object(runner.store, 'to_csv', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): runner.run([event(1)])
            retry = CollectionRunner(client, folder, config, report=lambda _: None)
            self.assertTrue(retry.run().status.eq('complete').all())
            self.assertEqual(client.calls, [1])

    def test_incomplete_lineup_is_not_marked_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(); client.incomplete.add(1)
            config = CollectionConfig(limit=1)
            runner = CollectionRunner(client, folder, config, report=lambda _: None)
            self.assertEqual(runner.run([event(1)]).status.tolist(), ['failed'])
            self.assertFalse(runner.store.exists)
            client.incomplete.clear()
            self.assertEqual(CollectionRunner(client, folder, config, report=lambda _: None).run().status.tolist(), ['complete'])
            self.assertEqual(client.calls, [1, 1])

    def test_config_cannot_reuse_another_collection_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            CollectionRunner(Client(), folder, CollectionConfig(limit=1), report=lambda _: None).run([event(1)])
            runner = CollectionRunner(Client(), folder, CollectionConfig(limit=2), report=lambda _: None)
            with self.assertRaisesRegex(ValueError, 'different collection'):
                runner.run([event(1), event(2)])


if __name__ == '__main__': unittest.main()

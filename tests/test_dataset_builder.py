import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import dataset_builder
import main
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa


class DatasetBuildTests(unittest.TestCase):
    def test_cached_collection_exports_and_packages_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            source = collection_directory(data, 'premier-league-2024-25')
            source.mkdir(parents=True)
            atomic_json(data / '.soccer-scraper.json', {'format': 1})
            atomic_json(source / 'job.json', {
                'competition': 'Premier League', 'season': '2024/25',
                'providers': ['sofascore'], 'spatial': []})
            runner = SeasonRunner({'sofascore': Sofa()}, source, providers=['sofascore'],
                                  spatial=(), report=lambda _: None)
            runner.run('EPL', '2024', limit=1, build_csvs=False)
            before = {p: p.read_bytes() for p in (source / 'match_checkpoints').rglob('*')
                      if p.is_file() and p.name != 'CURRENT.json'}
            self.assertTrue(before)
            with patch('providers.httpClient.HttpClient._request', side_effect=AssertionError('network')), \
                    patch.multiple(dataset_builder.config, BUILD_EVENTS=True,
                        BUILD_PLAYER_MATCH_STATS=True, BUILD_TEAM_MATCH_STATS=True,
                        BUILD_FIXTURES=True, BUILD_PLAYER_IDS=True, BUILD_TEAM_IDS=True, BUILD_MATCH_IDS=True):
                self.assertEqual(dataset_builder.build_dataset(data, root / 'dataset'), 0)
            packaged = root / 'dataset/premier-league/2024-2025'
            self.assertEqual({p.name for p in packaged.iterdir()}, {
                'events.csv', 'player_match_stats.csv', 'team_match_stats.csv', 'fixtures.csv'})
            self.assertTrue((root / 'dataset/player_ids.csv').is_file())
            self.assertTrue((root / 'dataset/team_ids.csv').is_file())
            self.assertTrue((root / 'dataset/match_ids.csv').is_file())
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertEqual(set(pd.read_csv(packaged / 'player_match_stats.csv').season), {'2024-2025'})

    def test_failed_export_keeps_previous_dataset_and_skips_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = collection_directory(root / 'data', 'bundesliga-2024-25')
            source.mkdir(parents=True)
            (source / 'job.json').write_text('{}')
            old = root / 'dataset/bundesliga/2024-2025/events.csv'
            old.parent.mkdir(parents=True)
            old.write_text('previous dataset\n')
            with patch('collection.cli.main', return_value=2), \
                    patch('dataset_builder.package_collection') as package, \
                    patch('dataset_builder.build_id_tables') as ids:
                self.assertEqual(dataset_builder.build_dataset(root / 'data', root / 'dataset'), 2)
            package.assert_not_called()
            ids.assert_not_called()
            self.assertEqual(old.read_text(), 'previous dataset\n')

    def test_builder_rejects_output_overlapping_raw_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'separate'):
                dataset_builder.build_dataset(folder, Path(folder) / 'dataset')

    def test_dataset_flag_includes_completed_skipped_seasons(self):
        with tempfile.TemporaryDirectory() as folder:
            source = collection_directory(folder, 'bundesliga-2024-25')
            source.mkdir(parents=True)
            (source / 'job.json').write_text('{}')
            with patch('main.COMPETITIONS', ['Bundesliga']), patch('main.SEASONS', ['2024/25']), \
                    patch.dict(main.config.TRANSPORT,{'sofascore_mode':'auto'}), \
                    patch('main._collection_complete', return_value=True), \
                    patch('main.collect') as collect, \
                    patch('dataset_builder.build_dataset', return_value=0) as build:
                self.assertEqual(main.main(['--build-dataset', '--data-root', folder]), 0)
            collect.assert_not_called()
            self.assertEqual(build.call_args.args[2], ['bundesliga-2024-25'])

    def test_export_routes_once_to_full_builder_even_with_flag_before_command(self):
        with patch('main.collect') as collect, \
                patch('dataset_builder.build_dataset', return_value=0) as build:
            self.assertEqual(main.main(['--build-dataset', 'export', '--competition',
                                        'Bundesliga', '--season', '2024/25']), 0)
        collect.assert_not_called()
        self.assertEqual(build.call_args.args[2], ['bundesliga-2024-25'])

    def test_disabled_flag_and_status_do_not_build(self):
        with patch('main.collect', return_value=0), patch('dataset_builder.build_dataset') as build:
            self.assertEqual(main.main(['status', '--build-dataset']), 0)
            self.assertEqual(main.main(['resume', '--no-build-dataset']), 0)
        build.assert_not_called()

    def test_partial_collection_still_packages_available_checkpoints(self):
        with patch('main.collect', return_value=2), \
                patch('dataset_builder.build_dataset', return_value=0) as build:
            self.assertEqual(main.main(['resume', '--build-dataset']), 2)
        build.assert_called_once()

    def test_interrupt_does_not_start_dataset_build(self):
        with patch('main.collect', return_value=130), patch('dataset_builder.build_dataset') as build:
            self.assertEqual(main.main(['resume', '--build-dataset']), 130)
        build.assert_not_called()

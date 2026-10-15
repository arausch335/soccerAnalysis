import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dataset_builder
import config as dataset_config
from collection.paths import collection_directory


DISABLED = dict(BUILD_EVENTS=False, BUILD_PLAYER_MATCH_STATS=False,
                BUILD_TEAM_MATCH_STATS=False, BUILD_FIXTURES=False,
                BUILD_PLAYER_IDS=False, BUILD_TEAM_IDS=False, BUILD_MATCH_IDS=False)


class DatasetConfigTests(unittest.TestCase):
    def source(self, root):
        source = collection_directory(root / 'data', 'bundesliga-2024-25')
        source.mkdir(parents=True)
        (source / 'job.json').write_text('{}')
        (source / 'config.json').write_text(json.dumps({
            'competition': 'Bundesliga', 'season': '2024/2025'}))
        (source / 'schedule.json').write_text(json.dumps({'events': [{
            'id': 1, 'startTimestamp': 1725148800, 'homeTeam': {'id': 1},
            'awayTeam': {'id': 2}, 'status': {'type': 'finished'}}]}))
        return source

    def test_fixture_only_run_needs_no_other_exports_and_preserves_disabled_table(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.source(root)
            output = root / 'dataset/bundesliga/2024-2025'
            output.mkdir(parents=True)
            (output / 'events.csv').write_text('existing event data\n')
            with patch.multiple(dataset_config, **{**DISABLED, 'BUILD_FIXTURES': True}), \
                    patch('collection.cli.main') as export:
                self.assertEqual(dataset_builder.build_dataset(root / 'data', root / 'dataset'), 0)
            export.assert_not_called()
            self.assertTrue((output / 'fixtures.csv').is_file())
            self.assertEqual((output / 'events.csv').read_text(), 'existing event data\n')
            self.assertFalse((output / 'player_match_stats.csv').exists())

    def test_disabled_events_skip_compilation_and_package_only_requested_types(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.source(root)
            with patch.multiple(dataset_config, **{**DISABLED, 'BUILD_PLAYER_MATCH_STATS': True}), \
                    patch('collection.cli.main', return_value=0) as export, \
                    patch('dataset_builder.package_collection') as package:
                self.assertEqual(dataset_builder.build_dataset(root / 'data', root / 'dataset'), 0)
            self.assertIn('--no-compile-events', export.call_args.args[0])
            self.assertEqual(package.call_args.kwargs['tables'], ('player_match_stats',))

    def test_reference_only_config_skips_exports_and_selects_id_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.multiple(dataset_config, **{**DISABLED, 'BUILD_PLAYER_IDS': True}), \
                    patch('collection.cli.main') as export, \
                    patch('dataset_builder.build_id_tables', return_value={}) as ids:
                self.assertEqual(dataset_builder.build_dataset(root / 'data', root / 'dataset'), 0)
            export.assert_not_called()
            self.assertEqual(ids.call_args.kwargs, dict(
                build_player_ids=True, build_team_ids=False, build_match_ids=False))

    def test_ide_run_uses_config_paths_and_season_selection_without_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.source(root)
            with patch.multiple(dataset_config, DATA_ROOT=root / 'data', DATASET_ROOT=root / 'dataset',
                                COLLECTION_NAMES=('bundesliga-2024-25',),
                                **{**DISABLED, 'BUILD_FIXTURES': True}), \
                    patch('dataset_builder.package_collection') as package:
                self.assertEqual(dataset_builder.main([]), 0)
            self.assertEqual(package.call_args.args, (source.resolve(), (root / 'dataset').resolve()))

    def test_invalid_switch_stops_before_writes(self):
        with patch.object(dataset_config, 'BUILD_EVENTS', 'False'), \
                patch('collection.cli.main') as export:
            with self.assertRaisesRegex(ValueError, 'True or False'):
                dataset_builder.build_dataset()
        export.assert_not_called()

    def test_all_types_disabled_stops_before_writes(self):
        with patch.multiple(dataset_config, **DISABLED), patch('collection.cli.main') as export:
            with self.assertRaisesRegex(ValueError, 'at least one'):
                dataset_builder.build_dataset()
        export.assert_not_called()

    def test_main_ide_run_uses_shared_paths_without_arguments(self):
        import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.source(root)
            with patch.multiple(dataset_config, DATA_ROOT=root / 'data', DATASET_ROOT=root / 'dataset'), \
                    patch.multiple(main, BUILD_DATASET=True, COMPETITIONS=['Bundesliga'], SEASONS=['2024/25']), \
                    patch('main._collection_complete', return_value=True), \
                    patch('main.collect') as collect, \
                    patch('dataset_builder.build_dataset', return_value=0) as build:
                self.assertEqual(main.main([]), 0)
            collect.assert_not_called()
            self.assertEqual(build.call_args.args, (
                root / 'data', root / 'dataset', ['bundesliga-2024-25']))

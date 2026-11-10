import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import dataset_pipeline.build as dataset_builder
import main
from contextlib import nullcontext
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa


class DatasetBuildTests(unittest.TestCase):
    def setUp(self):
        patcher=patch('main.ensemble_session',side_effect=lambda *a,**k:nullcontext())
        patcher.start();self.addCleanup(patcher.stop)

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
            packaged = root / 'dataset/raw_data/premier-league/2024-2025'
            self.assertEqual({p.name for p in packaged.iterdir()}, {
                'player_match_stats.csv', 'team_match_stats.csv', 'fixtures.csv'})
            self.assertTrue((root / 'dataset/concordance/player_ids/part-00000.parquet').is_file())
            self.assertTrue((root / 'dataset/concordance/team_ids/part-00000.parquet').is_file())
            self.assertTrue((root / 'dataset/concordance/match_ids/part-00000.parquet').is_file())
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertEqual(set(pd.read_csv(packaged / 'player_match_stats.csv').season), {'2024-2025'})

    def test_failed_export_keeps_previous_dataset_and_skips_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = collection_directory(root / 'data', 'bundesliga-2024-25')
            source.mkdir(parents=True)
            (source / 'job.json').write_text(json.dumps({'workflow_version':3,'providers':['sofascore','fotmob','whoscored']}))
            (source/'config.json').write_text(json.dumps({'competition':'Bundesliga','season':'2024/2025'}))
            (source/'schedule.json').write_text(json.dumps({'events':[]}))
            old = root / 'dataset/bundesliga/2024-2025/events.csv'
            old.parent.mkdir(parents=True)
            old.write_text('previous dataset\n')
            with patch('collection.cli.main', return_value=2), \
                    patch('dataset_pipeline.build.package_collection') as package, \
                    patch('dataset_pipeline.build.build_id_tables') as ids:
                self.assertEqual(dataset_builder.build_dataset(root / 'data', root / 'dataset'), 2)
            package.assert_not_called()
            ids.assert_not_called()
            self.assertEqual(old.read_text(), 'previous dataset\n')

    def test_builder_rejects_output_overlapping_raw_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'separate'):
                dataset_builder.build_dataset(folder, Path(folder) / 'dataset')

    def test_legacy_build_flag_points_to_explicit_package_command(self):
        with patch('dataset_pipeline.build.build_dataset') as build,patch('main.collect') as collect:
            with self.assertRaises(SystemExit):main.main(['resume','--build-dataset'])
        build.assert_not_called();collect.assert_not_called()

    def test_normal_collection_and_raw_export_never_package(self):
        for action,result in [('resume',0),('resume',2),('resume',130),('export',0),('status',0)]:
            with self.subTest(action=action,result=result),patch('main.collect',return_value=result),patch('dataset_pipeline.build.build_dataset') as build:
                self.assertEqual(main.main([action]),result)
                build.assert_not_called()

    def test_only_package_entrypoint_routes_to_dataset_production(self):
        with patch('dataset_pipeline.build.main',return_value=0) as package,patch('main.collect') as collect:
            self.assertEqual(main.entrypoint(['--package','--parquet']),0)
            package.assert_called_once_with(['--parquet'],package=True)
            collect.assert_not_called()

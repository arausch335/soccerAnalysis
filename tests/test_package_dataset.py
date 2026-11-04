from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import dataset_builder
import main
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from concordance.parquet import read_table, validate, CORE_COLUMNS
from concordance.registry import Registry
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa


class PackageDatasetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'data'
        self.output = self.base / 'dataset'
        self.output.mkdir()
        registry = Registry(self.root / '_system/identity/alex_ids.json')
        home = registry.assign('team', {'sofascore_id': '44'})
        away = registry.assign('team', {'sofascore_id': '2'})
        match = registry.assign('match', {'sofascore_id': '1'})
        registry.save()
        self.match_id = match['entity_id']
        pd.DataFrame([{
            'entity_id': self.match_id, 'alex_match_id': match['alex_id'],
            'sofascore_match_id': '1', 'date': '2024-08-17',
            'kickoff_utc': '2024-08-17T12:30:00+00:00', 'season': '2024-2025',
            'competition': 'Premier League', 'status': 'finished',
            'home_entity_id': home['entity_id'], 'away_entity_id': away['entity_id'],
            'alex_home_team_id': home['alex_id'], 'alex_away_team_id': away['alex_id'],
            'sofascore_home_team_id': '44', 'sofascore_away_team_id': '2',
            'home_team_id': '44', 'away_team_id': '2',
            'home_team': 'Liverpool FC', 'away_team': 'Other',
            'home_score': 0, 'away_score': 0, 'has_player_stats': True,
            'is_scheduled_for_scraping': False
        }]).to_csv(self.output / 'fixtures.csv', index=False)

    def test_entrypoint_reexports_compiled_data_without_workers_or_network(self):
        before = (self.output / 'fixtures.csv').read_bytes()
        with patch('main.main', side_effect=AssertionError('worker/collection startup')), \
                patch('main.acquire_bridge', side_effect=AssertionError('browser startup')), \
                patch('main.ensemble_session', side_effect=AssertionError('transfer startup')), \
                patch('providers.httpClient.HttpClient._request', side_effect=AssertionError('network')):
            code = main.entrypoint(['--package', '--data-root', str(self.root),
                                    '--dataset-root', str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual((self.output / 'concordance/metadata/compiled/fixtures.csv').read_bytes(), before)
        self.assertEqual({p.name for p in self.output.iterdir()}, {'concordance','coverage.json','.dataset-install.lock','dataset-manifest.json','read_dataset.R','read_concordances.R','README.md'})
        fixtures = read_table(self.output / 'concordance', 'fixtures/fixtures')
        self.assertEqual(tuple(fixtures.columns), CORE_COLUMNS)
        self.assertEqual(fixtures.match_entity_id.tolist(), [self.match_id])
        self.assertEqual(validate(self.output / 'concordance')['rows'], 1)
        self.assertFalse(validate(self.output / 'concordance')['ready_for_R'])

    def test_packages_saved_seasons_outside_config_selection_and_keeps_inputs(self):
        source = collection_directory(self.root, 'premier-league-2024-25')
        source.mkdir(parents=True)
        atomic_json(self.root / '.soccer-scraper.json', {'format': 1})
        atomic_json(source / 'job.json', {'competition': 'Premier League', 'season': '2024/25',
                    'providers': ['sofascore'], 'spatial': []})
        runner = SeasonRunner({'sofascore': Sofa()}, source, providers=['sofascore'],
                              spatial=(), report=lambda _: None)
        runner.run('EPL', '2024', limit=1, build_csvs=False)
        before = {p: p.read_bytes() for p in (source / 'match_checkpoints').rglob('*')
                  if p.is_file() and p.name != 'CURRENT.json'}
        with patch.multiple(dataset_builder.config, COLLECTION_NAMES=('unrelated-2023-24',),
                REFRESH_PUBLISHED_TEAM_IDS=True, BUILD_EVENTS=True), \
                patch('providers.reep.refresh', side_effect=AssertionError('identity download')), \
                patch('providers.httpClient.HttpClient._request', side_effect=AssertionError('network')):
            self.assertEqual(dataset_builder.package_dataset(self.root, self.output), 0)
        packaged = self.output / 'raw_data/premier-league/2024-2025'
        self.assertTrue((packaged / 'player_match_stats.csv').is_file())
        self.assertFalse((packaged / 'events.csv').exists())
        players = read_table(self.output / 'concordance', 'player_ids')
        self.assertEqual(set(players.sofascore_id), {'7', '8'})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_failed_season_does_not_replace_previous_parquet(self):
        dataset_builder.package_dataset(self.root, self.output)
        manifest = self.output / 'concordance/metadata/manifest.json'
        before = manifest.read_bytes()
        with patch('dataset_builder.build_dataset', return_value=2):
            self.assertEqual(dataset_builder.package_dataset(self.root, self.output,
                             ['premier-league-2024-25']), 2)
        self.assertEqual(manifest.read_bytes(), before)

    def test_empty_inputs_and_overlapping_output_fail_without_starting_collection(self):
        with self.assertRaisesRegex(ValueError, 'No saved seasons'):
            dataset_builder.package_dataset(self.base / 'empty', self.base / 'missing')
        with self.assertRaisesRegex(ValueError, 'separate'):
            dataset_builder.package_dataset(self.root, self.root / 'dataset')

    def test_repeated_build_keeps_one_layout_and_stable_ids(self):
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        ids=pd.read_parquet(self.output/'concordance/match_ids/part-00000.parquet').entity_id.tolist()
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        self.assertEqual(pd.read_parquet(self.output/'concordance/match_ids/part-00000.parquet').entity_id.tolist(),ids)
        self.assertEqual({p.name for p in (self.output/'concordance').iterdir()},
                         {'fixtures','match_ids','team_ids','player_ids','metadata'})
        self.assertFalse(list(self.output.glob('raw_data/*/*/events.csv')))

    def test_old_parquet_only_publication_migrates_to_csv_without_losing_ids(self):
        import json,shutil
        import pyarrow as pa
        import pyarrow.parquet as pq
        from concordance.publication import TABLES,read_table,digest
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        base=self.output/'concordance'
        manifest=json.loads((base/'metadata/manifest.json').read_text())
        old=self.output/'concordances';old.mkdir()
        manifest['format']='soccer-concordances-parquet-v1'
        for name in TABLES:
            frame=read_table(base,name)
            path=old/name/'part-00000.parquet';path.parent.mkdir(parents=True,exist_ok=True)
            pq.write_table(pa.Table.from_pandas(frame,preserve_index=False),path)
            manifest['tables'][name]={'rows':len(frame),'files':[{'path':path.relative_to(old).as_posix(),'sha256':digest(path)}]}
        (old/'manifest.json').write_text(json.dumps(manifest))
        shutil.rmtree(base)
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        self.assertEqual(pd.read_parquet(base/'fixtures/part-00000.parquet').match_entity_id.tolist(),[self.match_id])
        self.assertFalse(old.exists())
        self.assertTrue((base/'metadata/legacy/concordances/manifest.json').is_file())

    def test_failed_directory_swap_keeps_previous_csv_and_private_metadata(self):
        import concordance.publication as publication
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        base=self.output/'concordance'
        before=(base/'metadata/manifest.json').read_bytes()
        replace=publication.atomic_replace
        def interrupt(source,target):
            if Path(source).name=='bundle':raise OSError('interrupted directory swap')
            return replace(source,target)
        with patch('concordance.publication.atomic_replace',side_effect=interrupt):
            self.assertEqual(dataset_builder.package_dataset(self.root,self.output,mode='rebuild'),2)
        self.assertEqual((base/'metadata/manifest.json').read_bytes(),before)
        self.assertTrue((base/'metadata/compiled/fixtures.csv').is_file())
        self.assertEqual(publication.validate(base)['rows'],1)

    def test_compiled_only_migration_retains_old_season_csvs_without_events(self):
        season=self.output/'premier-league/2024-2025'
        season.mkdir(parents=True)
        for name in ('fixtures','player_match_stats','team_match_stats','events'):
            (season/(name+'.csv')).write_text('match_id,season\n1,2024-2025\n')
        self.assertEqual(dataset_builder.package_dataset(self.root,self.output),0)
        new=self.output/'raw_data/premier-league/2024-2025'
        self.assertEqual({p.name for p in new.iterdir()},
                         {'fixtures.csv','player_match_stats.csv','team_match_stats.csv'})
        self.assertFalse(season.exists())
        self.assertTrue((self.output/'concordance/metadata/legacy/premier-league/2024-2025/events.csv').exists())


if __name__ == '__main__':
    unittest.main()

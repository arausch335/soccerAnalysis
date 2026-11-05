"""Saved-data packaging skips unchanged inputs and can force a full regeneration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import dataset_builder
from collection.paths import collection_directory
from storage.pandasStore import atomic_json
from tests.test_providers import event


class PackageModeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'data';self.output=self.base/'dataset'
        self.sources=[]
        for competition,name,mid in [('Premier League','premier-league-2024-25',101),
                                     ('Bundesliga','bundesliga-2024-25',201)]:
            source=collection_directory(self.root,name);source.mkdir(parents=True)
            settings={'competition':competition,'season':'2024/2025','providers':['sofascore']}
            atomic_json(source/'config.json',settings);atomic_json(source/'job.json',settings)
            atomic_json(source/'schedule.json',{'events':[event(mid)]})
            self.sources.append(source)
        settings=patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=False,
                                BUILD_TEAM_MATCH_STATS=False,BUILD_FIXTURES=True)
        settings.start();self.addCleanup(settings.stop)
        self.state=self.output/'concordance/metadata/package-state.json'

    def build(self,mode='incremental'):
        return dataset_builder.package_dataset(self.root,self.output,mode=mode)

    def test_unchanged_run_skips_season_exports_id_scans_and_publication(self):
        self.assertEqual(self.build(),0)
        before=self.state.read_bytes()
        with patch('dataset_builder.package_collection',side_effect=AssertionError('unchanged season')), \
             patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged IDs')), \
             patch('concordance.publication.refresh',side_effect=AssertionError('unchanged publication')):
            self.assertEqual(self.build(),0)
        self.assertEqual(self.state.read_bytes(),before)

    def test_changed_season_only_is_processed_and_existing_rows_are_not_duplicated(self):
        self.assertEqual(self.build(),0)
        atomic_json(self.sources[1]/'schedule.json',{'events':[event(201),event(202)]})
        with patch('dataset_builder.package_collection',wraps=dataset_builder.package_collection) as package, \
             patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as ids:
            self.assertEqual(self.build(),0)
        self.assertEqual([call.args[0].parts[-2:] for call in package.call_args_list],[self.sources[1].parts[-2:]])
        self.assertEqual([call.args[0].parts[-2:] for call in ids.call_args_list],[self.sources[1].parts[-2:]])
        fixtures=pd.read_parquet(self.output/'concordance/fixtures/part-00000.parquet')
        self.assertEqual(len(fixtures),3);self.assertFalse(fixtures.match_entity_id.duplicated().any())
        self.assertEqual(self.build(),0)

    def test_new_season_is_added_without_reprocessing_existing_seasons(self):
        self.assertEqual(self.build(),0)
        source=collection_directory(self.root,'la-liga-2024-25');source.mkdir(parents=True)
        settings={'competition':'La Liga','season':'2024/2025','providers':['sofascore']}
        atomic_json(source/'config.json',settings);atomic_json(source/'job.json',settings)
        atomic_json(source/'schedule.json',{'events':[event(301)]})
        with patch('dataset_builder.package_collection',wraps=dataset_builder.package_collection) as package, \
             patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as ids:
            self.assertEqual(self.build(),0)
        self.assertEqual([call.args[0].parts[-2:] for call in package.call_args_list],[source.parts[-2:]])
        self.assertEqual([call.args[0].parts[-2:] for call in ids.call_args_list],[source.parts[-2:]])
        self.assertEqual(len(pd.read_parquet(self.output/'concordance/fixtures/part-00000.parquet')),3)

    def test_rebuild_ignores_caches_and_preserves_stable_ids(self):
        self.assertEqual(self.build(),0)
        ids_before=pd.read_parquet(self.output/'concordance/match_ids/part-00000.parquet').entity_id.tolist()
        with patch('dataset_builder.package_collection',wraps=dataset_builder.package_collection) as package, \
             patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as ids:
            self.assertEqual(self.build('rebuild'),0)
        self.assertEqual(package.call_count,2);self.assertEqual(ids.call_count,2)
        self.assertEqual(pd.read_parquet(self.output/'concordance/match_ids/part-00000.parquet').entity_id.tolist(),ids_before)

    def test_missing_output_is_repaired_without_rescanning_unchanged_ids(self):
        self.assertEqual(self.build(),0)
        missing=self.output/'raw_data/bundesliga/2024-2025/fixtures.csv';missing.unlink()
        with patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged IDs')):
            self.assertEqual(self.build(),0)
        self.assertTrue(missing.is_file())

    def test_failed_changed_season_does_not_advance_checkpoint(self):
        self.assertEqual(self.build(),0);before=json.loads(self.state.read_text())['seasons']
        atomic_json(self.sources[1]/'schedule.json',{'events':[event(201),event(202)]})
        with patch('dataset_builder.package_collection',side_effect=OSError('interrupted export')):
            self.assertEqual(self.build(),0)
        self.assertEqual(json.loads(self.state.read_text())['seasons'],before)
        self.assertEqual(self.build(),0)

    def test_cli_modes_are_exclusive_and_route_to_shared_builder(self):
        with patch('collection.datasetPackaging.package_saved',return_value=0) as build:
            self.assertEqual(dataset_builder.main(['--rebuild']),0)
            self.assertEqual(build.call_args.kwargs['mode'],'rebuild')
            self.assertEqual(dataset_builder.main(['--incremental']),0)
            self.assertEqual(build.call_args.kwargs['mode'],'incremental')
        with self.assertRaises(SystemExit):dataset_builder.main(['--rebuild','--incremental'])


if __name__=='__main__':unittest.main()

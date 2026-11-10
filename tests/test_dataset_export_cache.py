"""First-run failures retain completed seasons without certifying a publication."""
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import dataset_pipeline.build as dataset_builder
from dataset_pipeline.handoff import current
from storage.pandasStore import atomic_json
from tests import test_package_modes as fixtures
from tests.test_providers import event


class ExportResumeTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.PackageModeTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.root=self.case.root;self.output=self.case.output

    def fail_after_exports(self):
        with patch('dataset_pipeline.build._build_reference_csvs',side_effect=ValueError('later concordance failure')):
            self.assertEqual(self.case.build(),2)
        self.assertFalse(self.output.exists())
        return sorted((self.root/'_system/cache/dataset-exports').glob('*/*/manifest.json'))

    def test_first_package_failure_reuses_all_completed_exports_on_retry(self):
        caches=self.fail_after_exports();self.assertEqual(len(caches),2)
        with patch('dataset_pipeline.build.package_collection',side_effect=AssertionError('re-export')),patch('collection.cli.main',side_effect=AssertionError('raw export')):
            self.assertEqual(self.case.build(),0)
        self.assertTrue(current(self.output))
        self.assertTrue((self.output/'concordance/metadata/package-state.json').is_file())

    def test_checkpoint_statistics_export_is_restored_without_decoding_again(self):
        from tests.test_explicit_dataset import ExplicitDatasetTests
        from collection.datasetPackaging import package_saved
        from dataset_pipeline.review import load_review
        helper=ExplicitDatasetTests();helper.setUp();self.addCleanup(helper.doCleanups)
        settings=patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=True,BUILD_TEAM_MATCH_STATS=True)
        settings.start();self.addCleanup(settings.stop)
        source=helper.saved_match();before={p:p.read_bytes() for p in source.rglob('*') if p.is_file()}
        with patch('dataset_pipeline.build._build_reference_csvs',side_effect=ValueError('later publication failure')):
            self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),2)
        self.assertFalse(helper.output.exists())
        with patch('collection.cli.main',side_effect=AssertionError('checkpoint export repeated')),patch('dataset_pipeline.build.package_collection',side_effect=AssertionError('season export repeated')):
            self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),0)
        self.assertEqual(load_review(helper.output)['summary']['player_stat_matches'],1)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertFalse((source/'player_match_stats.csv').exists())

    def test_interruption_keeps_completed_seasons_and_retries_only_the_unfinished_one(self):
        original=dataset_builder.package_collection;calls=[]
        def interrupted(source,*args,**kwargs):
            calls.append(source.parent.name)
            if len(calls)==2:raise KeyboardInterrupt
            return original(source,*args,**kwargs)
        with patch('dataset_pipeline.build.package_collection',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):self.case.build()
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list((self.root/'_system/cache/dataset-exports').glob('*/*/manifest.json'))),1)
        with patch('dataset_pipeline.build.package_collection',wraps=original) as exported:
            self.assertEqual(self.case.build(),0)
        self.assertEqual([call.args[0].parent.name for call in exported.call_args_list],[calls[1]])

    def test_changed_inputs_invalidate_only_that_season(self):
        self.fail_after_exports()
        changed=self.case.sources[0];atomic_json(changed/'schedule.json',{'events':[event(101),event(102)]})
        with patch('dataset_pipeline.build.package_collection',wraps=dataset_builder.package_collection) as exported:
            self.assertEqual(self.case.build(),0)
        self.assertEqual([call.args[0].parts[-2:] for call in exported.call_args_list],[changed.parts[-2:]])

    def test_corrupt_cached_exports_are_rebuilt_and_never_installed_unchecked(self):
        caches=self.fail_after_exports()
        broken=caches[0].parent/'dataset/fixtures.csv';broken.write_text('corrupt cached output\n')
        with patch('dataset_pipeline.build.package_collection',wraps=dataset_builder.package_collection) as exported:
            self.assertEqual(self.case.build(),0)
        self.assertEqual(exported.call_count,1)
        self.assertTrue(current(self.output))

    def test_full_rebuild_explicitly_bypasses_the_recovery_cache(self):
        self.fail_after_exports()
        with patch('dataset_pipeline.build.package_collection',wraps=dataset_builder.package_collection) as exported:
            self.assertEqual(self.case.build('rebuild'),0)
        self.assertEqual(exported.call_count,2)

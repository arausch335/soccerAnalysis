"""Publish completed seasons, retain prior exports, and retry only unfinished work."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import dataset_builder
from dataset_handoff import current,verify
from dataset_review import load_review
from collection.datasetPackaging import package_saved
from collection.storageServer import Archive
from collection.workerIdentity import ensure_identity
from storage.pandasStore import atomic_json
from tests import test_package_modes as fixtures
from tests.test_providers import event


class PartialPackageTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.PackageModeTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.root=self.case.root;self.output=self.case.output
        self.bad=self.case.sources[1];self.good=self.case.sources[0]
        self.original=dataset_builder.package_collection
        self.messages=[]

    def report(self,*args,**kwargs):self.messages.append(str(args[0]))

    def build(self):return package_saved(self.root,self.output,report=self.report)

    def fail_one(self,source,*args,**kwargs):
        if source.parent.name==self.bad.parent.name:raise OSError('broken season export')
        return self.original(source,*args,**kwargs)

    def test_first_package_publishes_completed_season_then_diagnostic(self):
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        self.assertTrue(current(self.output));verify(self.output)
        self.assertTrue((self.output/'raw_data/premier-league/2024-2025/fixtures.csv').is_file())
        self.assertFalse((self.output/'raw_data/bundesliga/2024-2025').exists())
        review=load_review(self.output);diagnostic=review['package_diagnostic']
        self.assertEqual(diagnostic['status'],'partial');self.assertEqual(review['summary']['fixtures'],1)
        self.assertEqual(len(diagnostic['completed']),1);self.assertEqual(len(diagnostic['failed']),1)
        self.assertFalse(diagnostic['failed'][0]['retained_previous'])
        self.assertEqual(diagnostic['failed'][0]['error'],'broken season export')
        state=json.loads(self.case.state.read_text())
        self.assertNotIn('bundesliga-2024-25',state['seasons']);self.assertNotIn('bundesliga/2024-2025',state['source_inputs'])
        self.assertIn('Packaging diagnostic', '\n'.join(self.messages))

    def test_retry_only_rebuilds_failed_season_and_reuses_completed_id_index(self):
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        with patch('dataset_builder.package_collection',wraps=self.original) as exports,patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as ids:
            self.assertEqual(self.build(),0)
        self.assertEqual([c.args[0].parent.name for c in exports.call_args_list],['bundesliga'])
        self.assertEqual([c.args[0].parent.name for c in ids.call_args_list],['bundesliga'])
        self.assertEqual(load_review(self.output)['package_diagnostic']['status'],'complete')
        self.assertEqual(load_review(self.output)['summary']['fixtures'],2)

    def test_repeated_failure_skips_successful_exports_and_combined_rebuild(self):
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        before={p:p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
        with patch('dataset_builder.package_collection',side_effect=self.fail_one) as exports,patch('dataset_builder._source_id_rows',side_effect=AssertionError('reindex')),patch('concordance.publication.refresh',side_effect=AssertionError('rebuild')),patch('dataset_identity.annotate',side_effect=AssertionError('reannotate')),patch('dataset_review.create_review',side_effect=AssertionError('rescan coverage')):
            self.assertEqual(self.build(),0)
        self.assertEqual(exports.call_count,1)
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_failed_changed_season_retains_previous_export_and_checkpoint(self):
        self.assertEqual(self.build(),0)
        old=self.output/'raw_data/bundesliga/2024-2025/fixtures.csv';before=old.read_bytes()
        state=json.loads(self.case.state.read_text())['seasons']['bundesliga-2024-25']
        atomic_json(self.bad/'schedule.json',{'events':[event(201),event(202)]})
        atomic_json(self.good/'schedule.json',{'events':[event(101),event(102)]})
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        self.assertEqual(old.read_bytes(),before)
        self.assertEqual(json.loads(self.case.state.read_text())['seasons']['bundesliga-2024-25'],state)
        self.assertTrue(load_review(self.output)['package_diagnostic']['failed'][0]['retained_previous'])
        self.assertEqual(load_review(self.output)['summary']['fixtures'],3)
        self.assertTrue(current(self.output))

    def test_id_index_failure_excludes_new_export_then_reuses_it_after_fix(self):
        original=dataset_builder._source_id_rows
        def fail_ids(source,*args):
            if source.parent.name=='bundesliga':raise ValueError('ambiguous saved identity data')
            return original(source,*args)
        with patch('dataset_builder._source_id_rows',side_effect=fail_ids):self.assertEqual(self.build(),0)
        self.assertFalse((self.output/'raw_data/bundesliga/2024-2025').exists())
        self.assertEqual(load_review(self.output)['package_diagnostic']['failed'][0]['phase'],'ID indexing')
        with patch('dataset_builder.package_collection',side_effect=AssertionError('repeat export')),patch('dataset_builder._source_id_rows',wraps=original) as ids:
            self.assertEqual(self.build(),0)
        self.assertEqual(ids.call_count,1);self.assertEqual(load_review(self.output)['summary']['fixtures'],2)

    def test_repeated_id_failure_retains_publication_without_global_rebuild(self):
        original=dataset_builder._source_id_rows
        def fail_ids(source,*args):
            if source.parent.name=='bundesliga':raise ValueError('bad source IDs')
            return original(source,*args)
        with patch('dataset_builder._source_id_rows',side_effect=fail_ids):self.assertEqual(self.build(),0)
        with patch('dataset_builder._source_id_rows',side_effect=fail_ids) as ids,patch('dataset_builder.package_collection',side_effect=AssertionError('re-export')),patch('concordance.publication.refresh',side_effect=AssertionError('republish')),patch('dataset_review.create_review',side_effect=AssertionError('rescan')):
            self.assertEqual(self.build(),0)
        self.assertEqual(ids.call_count,1)

    def test_broken_new_config_uses_prior_namespace_for_retained_export(self):
        self.assertEqual(self.build(),0)
        before=(self.output/'raw_data/bundesliga/2024-2025/fixtures.csv').read_bytes()
        self.bad.joinpath('config.json').write_text('{broken config')
        atomic_json(self.good/'schedule.json',{'events':[event(101),event(102)]})
        self.assertEqual(self.build(),0)
        self.assertEqual((self.output/'raw_data/bundesliga/2024-2025/fixtures.csv').read_bytes(),before)
        self.assertTrue(load_review(self.output)['package_diagnostic']['failed'][0]['retained_previous'])
        self.assertTrue(current(self.output))

    def test_bad_schedule_and_missing_future_schedule_do_not_block_publication(self):
        self.bad.joinpath('schedule.json').write_text('{broken json')
        future=self.root/'ligue-1/2026-2027'
        atomic_json(future/'job.json',{'competition':'Ligue 1','season':'2026/2027'})
        atomic_json(future/'config.json',{'competition':'Ligue 1','season':'2026/2027'})
        self.assertEqual(self.build(),0)
        review=load_review(self.output)
        self.assertEqual(len(review['package_diagnostic']['failed']),1)
        self.assertEqual(len(review['deferred_seasons']),1);self.assertEqual(review['summary']['fixtures'],1)
        self.assertTrue(current(self.output))

    def test_archive_acknowledges_only_successful_seasons_after_publication(self):
        ensure_identity(self.root,'msi');archive=Archive(self.root)
        for season in ('premier-league/2024-2025','bundesliga/2024-2025'):archive.schedule_build(season)
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        self.assertEqual([r[0] for r in archive.build_jobs()],['bundesliga/2024-2025'])
        self.assertIsNotNone(archive.publication())
        self.assertLess(next(i for i,m in enumerate(self.messages) if 'Dataset published for Main' in m),next(i for i,m in enumerate(self.messages) if 'Packaging diagnostic' in m))

    @unittest.skipUnless(shutil.which('Rscript'),'Rscript unavailable')
    def test_partial_bundle_loads_in_r_using_only_dataset_directory(self):
        with patch('dataset_builder.package_collection',side_effect=self.fail_one):self.assertEqual(self.build(),0)
        copied=self.case.base/'R project'/'dataset';shutil.copytree(self.output,copied)
        program="""root=commandArgs(TRUE)[1];source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root))
x=read_dataset_table(ds,'fixtures')
stopifnot(nrow(x)==1,!anyNA(x$match_entity_id))
review=jsonlite::read_json(file.path(root,'coverage.json'),simplifyVector=TRUE)
stopifnot(review$package_diagnostic$status=='partial',nrow(review$package_diagnostic$failed)==1)
"""
        result=subprocess.run(['Rscript','--vanilla','-e',program,str(copied)],cwd=self.case.base,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_all_failed_first_package_has_diagnostic_and_no_false_publication(self):
        with patch('dataset_builder.package_collection',side_effect=OSError('no usable exports')):self.assertEqual(self.build(),2)
        self.assertFalse(self.output.exists())
        self.assertIn('no new dataset published','\n'.join(self.messages))

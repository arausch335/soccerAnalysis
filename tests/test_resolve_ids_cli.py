"""The built-in repair command operates on an existing dataset without rebuilding."""
from pathlib import Path
import csv
import json
import shutil
import unittest
from unittest.mock import patch

from filelock import FileLock

import main
from dataset_pipeline.resolve_ids import main as command,resolve_dataset
from dataset_pipeline.handoff import current,verify
from collection.storageServer import Archive
from storage.pandasStore import atomic_json
from concordance import package_state as tracking
from tests import test_r_handoff as fixture


class ResolveIdsCommandTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.RHandoffTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.case.write();self.dataset=self.case.dataset;self.source=self.case.base/'missing-source'

    def damaged(self):
        # Model an older dataset export with native IDs but no canonical columns.
        path=self.dataset/'raw_data/premier-league/2024-2025/player_match_stats.csv'
        with path.open(encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream);rows=list(reader);columns=reader.fieldnames
        keep=[c for c in columns if not c.endswith('_entity_id') and not c.startswith('identity_resolution_')]
        for row in rows:row['team_id']='9'
        with path.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=keep,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        return path

    def test_main_routes_to_resolver_without_workers_or_collection(self):
        with patch('dataset_pipeline.resolve_ids.main',return_value=0) as run,patch('main.acquire_bridge',side_effect=AssertionError('extension')),patch('collection.worker.entrypoint',side_effect=AssertionError('worker')):
            self.assertEqual(main.entrypoint(['--resolve-ids','--dataset-root','copied-dataset']),0)
        run.assert_called_once_with(['--dataset-root','copied-dataset'])
        with self.assertRaises(SystemExit):main.entrypoint(['--resolve-ids','--package'])

    def test_repairs_dataset_only_and_refreshes_audits_without_rebuild_or_publication(self):
        path=self.damaged();concordance={p:p.read_bytes() for p in (self.dataset/'concordance').rglob('*') if p.is_file()}
        with patch('dataset_pipeline.build.build_dataset',side_effect=AssertionError('dataset rebuild')),patch('collection.cli.main',side_effect=AssertionError('raw export')),patch('collection.storageServer.Archive.publish',side_effect=AssertionError('publication')),patch('providers.httpClient.HttpClient._request',side_effect=AssertionError('network')):
            self.assertEqual(command(['--dataset-root',str(self.dataset),'--data-root',str(self.source)]),0)
        self.assertFalse(self.source.exists())
        with path.open() as stream:rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['team_id'],'9');self.assertTrue(rows[0]['team_entity_id'])
        self.assertEqual(rows[0]['identity_resolution_status'],'resolved')
        self.assertTrue(current(self.dataset));verify(self.dataset)
        self.assertEqual(concordance,{p:p.read_bytes() for p in concordance})
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.dataset.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
        self.assertEqual(resolve_dataset(self.dataset,self.source,report=lambda *a,**k:None),0)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})

    def test_resolution_failure_and_install_failure_keep_previous_dataset(self):
        self.damaged();before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.dataset.rglob('*') if p.is_file()}
        with patch('dataset_pipeline.identity.annotate',side_effect=ValueError('invalid IDs')),self.assertRaisesRegex(ValueError,'invalid IDs'):
            resolve_dataset(self.dataset,self.source,report=lambda *a,**k:None)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})
        from collection.ensemble import atomic_copy
        copied=[]
        def fail_later(source,target):
            copied.append(target)
            if len(copied)==2:raise OSError('disk write failed')
            return atomic_copy(source,target)
        with patch('collection.ensemble.atomic_copy',side_effect=fail_later),self.assertRaisesRegex(OSError,'disk write failed'):
            resolve_dataset(self.dataset,self.source,report=lambda *a,**k:None)
        self.assertGreater(len(copied),1)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})
        extras={p for p in self.dataset.rglob('*') if p.is_file() and not p.name.endswith('.lock')}-before.keys()
        self.assertFalse(extras)

    def test_next_package_reuses_repaired_exports_and_publishes_them(self):
        from tests.test_explicit_dataset import ExplicitDatasetTests
        from collection.datasetPackaging import package_saved
        from collection.storageTransfer import identifier
        helper=ExplicitDatasetTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.saved_match();package_saved(helper.root,helper.output,report=lambda *a,**k:None)
        archive=Archive(helper.root);old=identifier(archive.publication())
        path=helper.output/'raw_data/premier-league/2024-2025/player_match_stats.csv'
        with path.open(newline='') as stream:
            reader=csv.DictReader(stream);rows=list(reader)
            columns=[c for c in reader.fieldnames if not c.endswith('_entity_id') and not c.startswith('identity_resolution_')]
        with path.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        self.assertEqual(resolve_dataset(helper.output,helper.root,report=lambda *a,**k:None),0)
        repaired=(path.read_bytes(),path.stat().st_mtime_ns)
        with patch('dataset_pipeline.build.package_collection',side_effect=AssertionError('season re-export')),patch('dataset_pipeline.build._source_id_rows',side_effect=AssertionError('ID re-index')):
            self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),0)
        self.assertNotEqual(old,identifier(archive.publication()))
        self.assertEqual(repaired,(path.read_bytes(),path.stat().st_mtime_ns))
        self.assertTrue(current(helper.output))

    def test_busy_dataset_and_missing_dataset_fail_clearly(self):
        with FileLock(str(self.dataset/'.dataset-install.lock')):
            with self.assertRaises(SystemExit) as error:command(['--dataset-root',str(self.dataset),'--data-root',str(self.source)])
        self.assertEqual(error.exception.code,2)
        with self.assertRaisesRegex(ValueError,'--package first'):resolve_dataset(self.case.base/'absent',self.source)

    def test_checkpoint_retains_inputs_but_requires_next_package_to_publish(self):
        path=self.damaged();state_path=self.dataset/'concordance/metadata/package-state.json'
        tracking.save(state_path,{'seasons':{'premier-league-2024-25':{'inputs':'saved-raw-inputs','recipe':'saved-recipe','outputs':{'player_match_stats':[0,0]}}},'archive_publication':'old-publication','global':'saved-global'})
        self.assertEqual(resolve_dataset(self.dataset,self.source,report=lambda *a,**k:None),0)
        state=tracking.load(state_path);season=state['seasons']['premier-league-2024-25']
        self.assertEqual(season['inputs'],'saved-raw-inputs');self.assertEqual(state['global'],'saved-global')
        self.assertEqual(season['outputs']['player_match_stats'],tracking.file_stamp(path))
        self.assertIsNone(state['archive_publication'])

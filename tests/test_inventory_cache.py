from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from collection.ensembleProgress import PortableProgress
from collection.inventoryMonitor import InventoryMonitor
from storage.pandasStore import PandasStore,atomic_json
from tests.test_ensemble import populate,quiet


class InventoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'data';populate(self.root,limit=1)

    def progress(self):return PortableProgress(None,self.root)

    def test_restart_reuses_validated_checkpoint_and_both_standalone_providers(self):
        first=self.progress();first.scan();expected=deepcopy(first.local_records)
        self.assertGreater(first.scan_summary['validated'],0)
        with patch.object(PandasStore,'from_csv',side_effect=AssertionError('Checkpoint reopened')), \
             patch('review_data.who_valid',side_effect=AssertionError('WhoScored revalidated')), \
             patch('collection.legacyFotmobRows.player_rows',side_effect=AssertionError('FotMob revalidated')):
            second=self.progress();second.scan()
        self.assertEqual(second.local_records,expected)
        self.assertEqual(second.scan_summary['validated'],0)
        self.assertEqual(second.scan_summary['reused'],first.scan_summary['validated'])

    def test_changed_checkpoint_revalidated_and_full_audit_bypasses_receipts(self):
        first=self.progress();first.scan()
        marker=next(self.root.glob('*/*/match_checkpoints/*/CURRENT.json'))
        store=PandasStore(marker.parent);dataset=store.from_csv();dataset.matches[1].metadata['changed']=True
        store.to_csv(dataset)
        next_scan=self.progress();next_scan.scan()
        self.assertEqual(next_scan.scan_summary['validated'],1)
        full=self.progress();full.scan(full=True)
        self.assertEqual(full.scan_summary['reused'],0)
        self.assertEqual(full.scan_summary['validated'],first.scan_summary['validated'])

    def test_changed_raw_fixture_is_revalidated_and_failed_receipt_is_removed(self):
        self.progress().scan()
        raw=next((self.root/'premier-league/2024-2025/raw/whoscored').glob('*.json'))
        saved=json.loads(raw.read_text());saved['payload']['match_id']=999;atomic_json(raw,saved)
        with self.assertRaisesRegex(ValueError,'Invalid WhoScored'):self.progress().scan()
        with self.assertRaisesRegex(ValueError,'Invalid WhoScored'):self.progress().scan()

    def test_removed_data_drops_stale_local_inventory_and_prunes_receipt(self):
        progress=self.progress();progress.scan()
        marker=next(self.root.glob('*/*/match_checkpoints/*/CURRENT.json'));shutil.rmtree(marker.parent)
        progress.scan()
        self.assertFalse(any(r['namespace']=='sofascore' for r in progress.local_records.values()))
        self.assertFalse(any(r['namespace']=='sofascore' for r in progress.records.values()))

    def test_identity_or_validation_rule_changes_force_revalidation(self):
        first=self.progress();first.scan()
        with patch('collection.inventoryCache.rules_version',return_value='changed-rules'):
            later=self.progress();later.scan()
        self.assertEqual(later.scan_summary['reused'],0)
        self.assertEqual(later.scan_summary['validated'],first.scan_summary['validated'])

    def test_copied_data_root_cannot_reuse_foreign_validation_receipts(self):
        first=self.progress();first.scan()
        copied=Path(self.temp.name)/'copied';shutil.copytree(self.root,copied)
        later=PortableProgress(None,copied);later.scan()
        self.assertEqual(later.scan_summary['reused'],0)

    def test_corrupt_receipt_database_is_rebuilt_from_real_data(self):
        first=self.progress();first.scan()
        (self.root/'_system/cache/inventory.sqlite').write_bytes(b'broken inventory cache')
        later=self.progress();later.scan()
        self.assertEqual(later.scan_summary['reused'],0)
        self.assertEqual(set(later.local_records),set(first.local_records))
        for key,row in first.local_records.items():
            self.assertEqual(later.local_records[key]['resources'],row['resources'])

    def test_monitor_supervisor_starts_background_checks_without_scraping(self):
        from tests.test_worker import WorkerTests,NoSync
        from collection.worker import run
        case=WorkerTests();case.setUp();self.addCleanup(case.doCleanups)
        class Stop:
            done=False
            def is_set(self):return self.done
            def wait(self,seconds):self.done=True;return True
        args=[x for x in case.args if x!='--once']+['--monitor']
        with patch('collection.worker.InventoryMonitor') as watch:
            watch.return_value.start.return_value.status={'state':'current'}
            code=run(args,runner=lambda args:self.fail('Monitor scraped'),session=case.session,
                     sync_factory=NoSync,stop=Stop())
            self.assertEqual(code,0);self.assertEqual(case.sessions,0)
            watch.return_value.start.assert_called_once()
            watch.return_value.start.return_value.close.assert_called_once()

    def test_main_monitor_rechecks_changes_and_publishes_without_collecting(self):
        changed=[];monitor=InventoryMonitor(self.root,Path(self.temp.name)/'ensemble','main',
            on_change=lambda:changed.append(True),report=quiet)
        self.assertTrue(monitor.check());self.assertEqual(len(changed),1)
        with patch.object(PandasStore,'from_csv',side_effect=AssertionError('Checkpoint reopened')):
            self.assertFalse(monitor.check())
        self.assertEqual(monitor.status['state'],'current')
        self.assertEqual(monitor.status['validated'],0)
        self.assertEqual(len(changed),1)
        marker=next(self.root.glob('*/*/match_checkpoints/*/CURRENT.json'));shutil.rmtree(marker.parent)
        self.assertTrue(monitor.check());self.assertEqual(len(changed),2)


if __name__=='__main__':unittest.main()

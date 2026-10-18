import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import main
from collection.ensemble import ensemble_session
from collection.ensembleProgress import job_key
from review_data import build_report
from storage.pandasStore import atomic_json
from tests.test_ensemble import populate, quiet


class WorkerInventoryTests(unittest.TestCase):
    def inventory(self, root, staging, name=None):
        args=['--inventory','--data-root',str(root),'--ensemble-root',str(staging)]
        if name is not None:args+=['--worker-id',name]
        return main.main(args)

    def test_named_inventory_is_offline_idempotent_and_contains_only_local_data(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';staging=base/'ensemble';populate(root,limit=1)
            with patch('main.collect',side_effect=AssertionError('Inventory must not scrape')):
                self.assertEqual(self.inventory(root,staging,'main-mac'),0)
            identity=json.loads((root/'identity.json').read_text())
            self.assertEqual(identity['worker_id'],'main-mac')
            worker=staging/'workers'/'main-mac.json';before=worker.read_bytes()
            saved=json.loads(before)
            self.assertEqual(saved['worker_id'],'main-mac');self.assertEqual(len(saved['records']),3)
            self.assertTrue(all(row['origins']==['main-mac'] for row in saved['records'].values()))
            self.assertEqual(self.inventory(root,staging),0)
            self.assertEqual(worker.read_bytes(),before)

    def test_git_worker_files_are_read_without_legacy_map_and_not_republished_as_local(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);laptop=base/'laptop';source=base/'source';populate(laptop,limit=1)
            with ensemble_session(laptop,source,quiet,worker_id='laptop'):pass
            staging=base/'ensemble';(staging/'workers').mkdir(parents=True)
            remote=staging/'workers'/'laptop.json'
            shutil.copyfile(source/'workers'/'laptop.json',remote);before=remote.read_bytes()
            root=base/'main-data'
            self.assertEqual(self.inventory(root,staging,'main-mac'),0)
            self.assertEqual(remote.read_bytes(),before)
            self.assertEqual(json.loads((staging/'workers'/'main-mac.json').read_text())['records'],{})
            (staging/'provenance.json').unlink()
            report=build_report(root,['EPL'],['2024/25'],staging/'provenance.json')
            self.assertEqual(set(report.complete),{0});self.assertEqual(set(report.portable_elsewhere),{1})
            with ensemble_session(root,staging,quiet) as progress:
                self.assertTrue(progress.done('sofascore','Premier League','2024/25',1,['provider:sofascore']))

    def test_import_retains_source_id_and_collector_origins_without_replacing_receiver(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';staging=base/'ensemble';laptop=base/'laptop-data'
            populate(root,limit=1);populate(laptop)
            with ensemble_session(root,staging,quiet,worker_id='main-mac'):pass
            with ensemble_session(laptop,base/'laptop-ensemble',quiet,worker_id='laptop'):pass
            (staging/'transfer').mkdir()
            shutil.copytree(laptop,staging/'transfer'/'data')
            self.assertEqual(self.inventory(root,staging),0)
            self.assertEqual(json.loads((root/'identity.json').read_text())['worker_id'],'main-mac')
            records=json.loads((staging/'workers'/'main-mac.json').read_text())['records']
            first=job_key('sofascore','Premier League','2024/25',1)
            second=job_key('sofascore','Premier League','2024/25',2)
            self.assertEqual(records[first]['origins'],['laptop','main-mac'])
            self.assertEqual(records[second]['origins'],['laptop'])
            archive=next((root/'ensemble_imports').iterdir())
            self.assertEqual(json.loads((archive/'import.json').read_text())['source_worker_id'],'laptop')
            self.assertEqual(json.loads((archive/'machine_state'/'identity.json').read_text())['worker_id'],'laptop')

    def test_inventory_refresh_removes_missing_artifacts_from_own_held_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';staging=base/'ensemble';populate(root,limit=1)
            self.assertEqual(self.inventory(root,staging,'main-mac'),0)
            raw=next((root/'whoscored_events'/'premier-league'/'2024-2025'/'raw'/'whoscored').glob('*.json'))
            raw.unlink()
            self.assertEqual(self.inventory(root,staging),0)
            rows=json.loads((staging/'workers'/'main-mac.json').read_text())['records']
            self.assertEqual(len(rows),2)
            self.assertNotIn('whoscored_events',{row['namespace'] for row in rows.values()})
            with ensemble_session(root,staging,quiet) as progress:
                self.assertFalse(progress.done('whoscored_events','Premier League','2024/25',11))

    def test_duplicate_worker_id_does_not_overwrite_another_workers_file(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);staging=base/'ensemble'
            with ensemble_session(base/'first',staging,quiet,worker_id='laptop'):pass
            worker=staging/'workers'/'laptop.json';before=worker.read_bytes()
            self.assertEqual(self.inventory(base/'second',staging,'laptop'),2)
            self.assertEqual(worker.read_bytes(),before)
            self.assertEqual(self.inventory(base/'second',staging,'desktop'),0)
            self.assertEqual(worker.read_bytes(),before)

    def test_user_can_set_identity_in_the_data_folder_and_invalid_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'data';staging=base/'ensemble';populate(root,limit=1)
            atomic_json(root/'identity.json',{'worker_id':'main-mac'})
            self.assertEqual(self.inventory(root,staging),0)
            self.assertTrue((staging/'workers'/'main-mac.json').exists())
            self.assertEqual(self.inventory(root,staging,'../unsafe'),2)
            self.assertEqual(json.loads((root/'identity.json').read_text())['worker_id'],'main-mac')


if __name__=='__main__':unittest.main()

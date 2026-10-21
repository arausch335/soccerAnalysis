from pathlib import Path
import tempfile
import unittest
from tests.test_ensemble import populate,quiet
from tools.prepare_transfer import prepare,verify_bundle
from collection.ensemble import import_pending
from collection.workerIdentity import ensure_identity,read_worker_id
from collection.ensembleProgress import PortableProgress
from storage.pandasStore import atomic_json,PandasStore


class PortableTransferTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        self.root=self.base/'main';populate(self.root,limit=1);ensure_identity(self.root,'main')
        atomic_json(self.root/'_system/config/reporting.json',{'token':'must-not-copy'})
        atomic_json(self.root/'_system/config/storage-secrets.json',{'secret':'must-not-copy'})
        self.bundle=self.base/'sd/main-data'
        self.bundle.parent.mkdir()
    def test_verified_bundle_excludes_machine_state_and_merges_preserving_receiver_identity(self):
        prepare(self.root,self.bundle,quiet);manifest=verify_bundle(self.bundle)
        self.assertEqual(manifest['worker'],'main');self.assertEqual(manifest['seasons'],1)
        self.assertTrue(list((self.bundle/'fotmob_player_stats').glob('*/*/raw/*/*.json')))
        self.assertFalse((self.bundle/'_system/config/reporting.json').exists())
        self.assertFalse((self.bundle/'_system/config/storage-secrets.json').exists())
        live=self.base/'headless';populate(live,limit=1);ensure_identity(live,'headless')
        atomic_json(live/'_system/config/reporting.json',{'token':'headless-kept'})
        import_pending(live,self.bundle.parent,quiet)
        self.assertFalse(self.bundle.exists());self.assertTrue((self.root/'.soccer-scraper.json').exists())
        self.assertEqual(read_worker_id(live),'headless')
        self.assertEqual((live/'_system/config/reporting.json').read_text(),'{"token": "headless-kept"}')
        progress=PortableProgress(None,live);progress.scan()
        self.assertTrue(any('main' in r['origins'] for r in progress.local_records.values()))
    def test_corruption_stops_import_before_receiver_changes(self):
        prepare(self.root,self.bundle,quiet)
        path=next(self.bundle.glob('*/*/raw/*/*.json'));path.write_text('{"corrupted":true}')
        live=self.base/'headless';atomic_json(live/'.soccer-scraper.json',{'format':1})
        with self.assertRaisesRegex(ValueError,'checksum'):import_pending(live,self.bundle.parent,quiet)
        self.assertTrue(self.bundle.exists());self.assertFalse(list(live.glob('*/*/CURRENT.json')))
    def test_existing_bundle_and_unknown_added_files_are_never_silently_overwritten(self):
        prepare(self.root,self.bundle,quiet)
        with self.assertRaisesRegex(ValueError,'already exists'):prepare(self.root,self.bundle,quiet)
        (self.bundle/'extra.txt').write_text('unknown')
        with self.assertRaisesRegex(ValueError,'unverified extra'):verify_bundle(self.bundle)
    def test_retired_checkpoint_generations_are_not_exported(self):
        directory=self.root/'premier-league/2024-2025/match_checkpoints/1'
        retired=directory/'snapshots'/('0'*32)/'matches.csv';retired.parent.mkdir();retired.write_text('retired')
        prepare(self.root,self.bundle,quiet)
        self.assertFalse((self.bundle/retired.relative_to(self.root)).exists())


if __name__=='__main__':unittest.main()

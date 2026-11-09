import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from collection.rawInventory import FORMAT, ROUTE
from collection.storageServer import StorageServer
from collection.storageTransfer import Peer, digest
from collection.workerIdentity import ensure_identity
from storage.pandasStore import atomic_json
from tools.raw_inventory import compare


class RawInventoryTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.main = Path(folder.name) / 'main'
        self.msi = Path(folder.name) / 'msi'
        for worker, root in [('main', self.main), ('msi', self.msi)]:
            atomic_json(root / '.soccer-scraper.json', {'format': 1})
            ensure_identity(root, worker)
        options = {'url': 'http://127.0.0.1:0', 'tokens': {
            'main': {'role': 'consumer', 'token': 'm' * 40},
            'headless': {'role': 'producer', 'token': 'h' * 40},
        }}
        server = StorageServer(self.msi, options, reserve=0).start()
        self.addCleanup(server.close)
        self.reader = Peer(server.url, 'm' * 40)
        self.producer = Peer(server.url, 'h' * 40)
        self.season = 'premier-league/2024-2025'
        self.vault = self.msi / '_system/storage/retained-raw'
        self.receipts = self.main / '_system/storage/raw-prune'

    def raw(self, root, letter, data=b'abc', season=None):
        path = root / (season or self.season) / 'raw/fotmob' / (letter * 64 + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def compare(self, *, apply=False, peer=None):
        compare(self.main, peer or self.reader, 'msi', apply=apply, report=lambda *a, **k: None)

    def test_preview_then_remove_only_exact_matches_with_recoverable_msi_bytes(self):
        match = self.raw(self.main, 'a')
        remote = self.raw(self.msi, 'a')
        conflict = self.raw(self.main, 'b', b'new')
        self.raw(self.msi, 'b', b'old')
        missing = self.raw(self.main, 'c')
        unrelated = self.main / self.season / 'job.json'
        atomic_json(unrelated, {'test': True})
        sha = digest(match)

        self.compare()
        self.assertTrue(match.exists())
        self.assertFalse(self.vault.exists())
        self.assertFalse(self.receipts.exists())

        self.compare(apply=True)
        self.assertFalse(match.exists())
        self.assertTrue(conflict.exists())
        self.assertTrue(missing.exists())
        self.assertTrue(unrelated.exists())
        [receipt] = self.receipts.glob('*.json')
        evidence = json.loads(receipt.read_text())
        self.assertEqual(evidence['files'], [{'path': match.relative_to(self.main).as_posix(),
                                              'size': 3, 'sha256': sha}])
        self.assertEqual(evidence['removed_count'], 1)
        replacement = remote.with_suffix('.tmp')
        replacement.write_bytes(b'xyz')
        replacement.replace(remote)
        self.assertEqual(digest(self.vault / sha), sha)

    def test_remote_failure_after_a_confirmed_season_leaves_all_main_files(self):
        first = self.raw(self.main, 'a')
        self.raw(self.msi, 'a')
        second = self.raw(self.main, 'b', season='serie-a/2024-2025')
        reader = self.reader

        class FailSecond:
            def request(self, route, value, **kwargs):
                if value['files'][0]['path'].startswith('serie-a/'):
                    raise OSError('MSI disconnected')
                return reader.request(route, value, **kwargs)

        with self.assertRaisesRegex(OSError, 'disconnected'):
            self.compare(apply=True, peer=FailSecond())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse(self.receipts.exists())

    def test_local_file_changed_after_verification_is_kept(self):
        first = self.raw(self.main, 'a')
        self.raw(self.msi, 'a')
        reader = self.reader

        class ChangeLocal:
            def request(self, route, value, **kwargs):
                result = reader.request(route, value, **kwargs)
                replacement = first.with_suffix('.tmp')
                replacement.write_bytes(b'new')
                replacement.replace(first)
                return result

        self.compare(apply=True, peer=ChangeLocal())
        self.assertEqual(first.read_bytes(), b'new')

    def test_failed_retention_leaves_main_copy_and_no_removal_receipt(self):
        first = self.raw(self.main, 'a')
        self.raw(self.msi, 'a')
        self.vault.mkdir()
        (self.vault / digest(first)).write_bytes(b'bad')
        with self.assertRaises(HTTPError) as caught:
            self.compare(apply=True)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 503)
        self.assertTrue(first.exists())
        self.assertFalse(self.receipts.exists())

    def test_protocol_rejects_invalid_paths_and_producer_role(self):
        path = self.raw(self.msi, 'a')
        valid = {'path': path.relative_to(self.msi).as_posix(), 'size': 3, 'sha256': digest(path)}
        for reader, row, status in [(self.producer, valid, 403),
                                    (self.reader, {**valid, 'path': '../outside.json'}, 400),
                                    (self.reader, {**valid, 'path': self.season + '/job.json'}, 400)]:
            with self.assertRaises(HTTPError) as caught:
                reader.request(ROUTE, {'format': FORMAT, 'retain': True, 'files': [row]})
            caught.exception.close()
            self.assertEqual(caught.exception.code, status)
        self.assertFalse(self.vault.exists())

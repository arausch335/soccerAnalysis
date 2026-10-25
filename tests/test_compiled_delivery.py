"""Compiled fixture delivery reaches Main directly, without the MSI raw route."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from collection.compiledDelivery import CompiledPeer, deliver, flush, outbox, receipt_path
from collection.reporting import ReportingServer
from collection.storageAgent import install_dataset
from collection.storageTransfer import FIXTURE_DATASET_PATHS, FORMAT, digest
from collection.workerIdentity import ensure_identity
from storage.layout import system_path
from storage.pandasStore import atomic_json


class CompiledDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'headless'
        ensure_identity(self.root, 'headless')
        self.main = self.base / 'main'; self.output = self.base / 'main-dataset'
        self.token = 'x' * 40
        self.server = ReportingServer(self.main, self.base / 'ensemble',
            {'bind': '127.0.0.1', 'port': 0, 'tokens': {'headless': self.token}},
            dataset_root=self.output).start()
        self.addCleanup(self.server.close)
        self.options = dict(mode='client', worker_id='headless',
            url=f'http://127.0.0.1:{self.server.server.server_port}', token=self.token)
        atomic_json(system_path(self.root, 'reporting.json'), self.options)
        self.source = self.base / 'worker-dataset'
        for name in FIXTURE_DATASET_PATHS:
            path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n' if path.suffix == '.json' else 'match_id,season\n1,2010-2011\n')

    def peer(self, instance=None):
        identity = json.loads(system_path(self.root, 'identity.json').read_text())
        return CompiledPeer(self.options['url'], self.token,
            headers={'X-Worker-Instance': instance or identity['instance_id']})

    def test_compiled_bundle_is_installed_on_main_before_ack_without_raw_outbox(self):
        raw = self.root / 'premier-league/2010-2011/raw/sofascore/match.json'
        atomic_json(raw, {'raw': 'stays on the worker'})
        result = deliver(self.root, self.source, report=lambda *a, **k: None)
        self.assertEqual(result['state'], 'installed_on_main')
        for name in FIXTURE_DATASET_PATHS:
            self.assertEqual((self.output / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual(json.loads(receipt_path(self.main).read_text())['route'], 'worker_to_main')
        self.assertFalse((self.root / '_system/storage/outbox').exists())
        self.assertTrue(raw.exists()); self.assertFalse(list(self.output.rglob('raw')))
        self.assertEqual(outbox(self.root, 'headless').queued(), 0)
        self.assertFalse(list((self.main / '_system/compiled/receiver/objects').glob('*')))

    def test_legacy_bundle_without_attempt_audit_is_still_accepted(self):
        (self.source / 'fixture_concordance/attempts.csv').unlink()
        audit=self.output / 'fixture_concordance/attempts.csv'
        audit.parent.mkdir(parents=True);audit.write_text('stale audit from a different snapshot')
        result = deliver(self.root, self.source, report=lambda *a, **k: None)
        self.assertEqual(result['state'], 'installed_on_main')
        self.assertTrue((self.output / 'fixtures.csv').exists())
        self.assertFalse(audit.exists())

    def test_main_reconciles_received_fixture_ids_with_its_current_index(self):
        from providers import reep
        from fixtures.identityRefresh import read_table
        rows=[['match','sofascore','1','rm1'],['match','whoscored','100','rm1']]
        reep.install(self.main,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        (self.source/'fixtures.csv').write_text(
            'sofascore_match_id,fotmob_match_id,whoscored_match_id,reep_match_id,home_team,away_team,match_identity_status,whoscored_fixture_check\n'
            '1,,,,Home,Away,unmapped,missing\n')
        atomic_json(self.source/'fixture_concordance/manifest.json',{'schedule_coverage_complete':False})
        result=deliver(self.root,self.source,report=lambda *a,**k:None)
        self.assertEqual(result['state'],'installed_on_main')
        _,received=read_table(self.output/'fixtures.csv')
        self.assertEqual(received[0]['whoscored_match_id'],'100')
        self.assertEqual(received[0]['whoscored_fixture_check'],'bridge_only')
        self.assertFalse(json.loads((self.output/'fixture_concordance/manifest.json').read_text())['schedule_coverage_complete'])

    def test_offline_bundle_remains_queued_and_retries_after_restart(self):
        atomic_json(system_path(self.root, 'reporting.json'), {**self.options, 'url': 'http://127.0.0.1:1'})
        result = deliver(self.root, self.source, report=lambda *a, **k: None)
        self.assertEqual(result['state'], 'queued')
        self.assertFalse((self.output / 'fixtures.csv').exists())
        atomic_json(system_path(self.root, 'reporting.json'), self.options)
        self.assertEqual(flush(self.root), 1)
        self.assertTrue((self.output / 'fixtures.csv').exists())

    def test_raw_or_arbitrary_dataset_upload_cannot_use_direct_compiled_endpoint(self):
        path = self.source / 'fixtures.csv'
        manifest = dict(format=FORMAT, kind='dataset', worker='headless', files=[
            dict(path='premier-league/2010-2011/players.csv', size=path.stat().st_size, sha256=digest(path))])
        with self.assertRaises(HTTPError) as caught:
            self.peer().request('/v1/uploads', manifest)
        self.assertEqual(caught.exception.code, 403)
        raw = self.root / '.soccer-scraper.json'; atomic_json(raw, {'format': 1})
        manifest.update(kind='raw', season='premier-league/2010-2011', completed=True)
        manifest['files'].append(dict(path='.soccer-scraper.json', size=raw.stat().st_size, sha256=digest(raw)))
        with self.assertRaises(HTTPError) as caught:
            self.peer().request('/v1/uploads', manifest)
        self.assertEqual(caught.exception.code, 403)

    def test_wrong_worker_instance_is_rejected_and_spool_retained(self):
        self.server.store.accept('headless', [dict(instance_id='registered', stream='test', sequence=1)])
        result = deliver(self.root, self.source, report=lambda *a, **k: None)
        self.assertEqual(result['state'], 'queued')
        self.assertFalse((self.output / 'fixtures.csv').exists())
        self.assertEqual(outbox(self.root, 'headless').queued(), 1)

    def test_checksum_failure_cannot_replace_main_files_and_can_resume(self):
        deliver(self.root, self.source, attempt=False, report=lambda *a, **k: None)
        queue = outbox(self.root, 'headless')
        with queue.connect() as db:
            mid, body = db.execute('SELECT id,manifest FROM queue').fetchone()
        item = json.loads(body)['files'][0]
        spool = queue.directory / mid / item['sha256']; original = spool.read_bytes()
        spool.write_bytes(b'x' * len(original))
        with self.assertRaises(HTTPError):flush(self.root)
        self.assertFalse((self.output / 'fixtures.csv').exists())
        spool.write_bytes(original)
        self.assertEqual(flush(self.root), 1)

    def test_msi_dataset_install_can_preserve_direct_main_fixture_snapshot(self):
        target = self.output / 'fixtures.csv'; target.parent.mkdir(parents=True); target.write_text('direct worker fixture snapshot')
        stale = self.source / 'fixtures.csv'
        manifest = dict(format=FORMAT, kind='dataset', worker='msi', files=[
            dict(path='fixtures.csv', size=stale.stat().st_size, sha256=digest(stale))])
        class NoDownload:
            def download(self, *args):raise AssertionError('MSI must not overwrite direct fixtures')
        install_dataset(NoDownload(), manifest, self.output, self.base / 'downloads', exclude=FIXTURE_DATASET_PATHS)
        self.assertEqual(target.read_text(), 'direct worker fixture snapshot')


if __name__ == '__main__':unittest.main()

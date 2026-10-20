from tests.paths import system_path
"""Legacy alias migration preserves data, provider identity and transfer origins."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from filelock import FileLock
from collection.ensemble import atomic_copy, ensemble_session, import_pending
from collection.whoScoredData import migrate_legacy
from collection.ensembleProgress import job_key
from storage.pandasStore import atomic_json
from tools.migrate_whoscored import main as migrate
from tests.test_whoscored_integration import populate, quiet


def legacy(root,subtree='whoscored',prefixed=False):
    canonical=populate(root)
    old=Path(root)/subtree/canonical.parent.name/canonical.name
    old.parent.mkdir(parents=True);canonical.rename(old)
    if not prefixed:
        for name in ('job.json','schedule.json'):(old/('whoscored_'+name)).rename(old/name)
    return old,canonical


class LegacyWhoScoredMigrationTests(unittest.TestCase):
    def test_whoscored_alias_migrates_exact_bytes_and_is_idempotent(self):
        for prefixed in (False,True):
            with self.subTest(prefixed=prefixed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'data';old,target=legacy(root,prefixed=prefixed)
                raw=next((old/'raw/whoscored').glob('*.json'));original=raw.read_bytes()
                self.assertEqual(migrate_legacy(root,quiet),1)
                self.assertEqual(next((target/'raw/whoscored').glob('*.json')).read_bytes(),original)
                self.assertTrue((target/'whoscored_job.json').exists())
                self.assertTrue((target/'whoscored_schedule.json').exists())
                self.assertFalse((target/'whoscored_whoscored_job.json').exists())
                self.assertFalse((root/'whoscored').exists())
                self.assertEqual(migrate_legacy(root,quiet),0)

    def test_raw_only_legacy_folder_moves_without_inventing_a_schedule_or_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root)
            (old/'job.json').unlink();(old/'schedule.json').unlink()
            raw=next((old/'raw/whoscored').glob('*.json'))
            # Earlier layouts used a flat raw folder and provider-native filename.
            original=raw.read_bytes();raw.rename(old/'raw/11.json');(old/'raw/whoscored').rmdir()
            self.assertEqual(migrate_legacy(root,quiet),1)
            self.assertEqual(next((target/'raw/whoscored').glob('*.json')).read_bytes(),original)
            self.assertFalse((target/'whoscored_schedule.json').exists())
            with ensemble_session(root,Path(tmp)/'ensemble',quiet) as progress:
                self.assertEqual(progress.local_records,{})

    def test_missing_job_is_reconstructed_from_verified_league_season_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root);(old/'job.json').unlink()
            migrate_legacy(root,quiet)
            with ensemble_session(root,Path(tmp)/'ensemble',quiet) as progress:
                self.assertEqual(len(progress.local_records),1)
            self.assertEqual(json.loads((target/'whoscored_job.json').read_text())['competition'],'Premier League')

    def test_both_legacy_names_merge_without_losing_conflicting_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root,'whoscored_events')
            other=root/'whoscored'/old.parent.name/old.name;shutil.copytree(old,other)
            raw=next((other/'raw/whoscored').glob('*.json'));changed=json.loads(raw.read_text())
            changed['payload']['matchCentreData']['events'].append({'id':100,'eventId':8,'teamId':501,'playerId':77})
            atomic_json(raw,changed)
            originals={next((folder/'raw/whoscored').glob('*.json')).read_bytes() for folder in (old,other)}
            self.assertEqual(migrate_legacy(root,quiet),2)
            preserved={next((target/'raw/whoscored').glob('*.json')).read_bytes()}
            for path in (system_path(root,"ensemble_imports")).rglob('*.json'):
                if 'payload' in json.loads(path.read_text()):preserved.add(path.read_bytes())
            self.assertTrue(originals<=preserved)
            self.assertEqual(len(json.loads(next((target/'raw/whoscored').glob('*.json')).read_text())['payload']['matchCentreData']['events']),2)

    def test_invalid_raw_provider_keeps_source_and_publishes_no_raw_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root)
            raw=next((old/'raw/whoscored').glob('*.json'));saved=json.loads(raw.read_text());saved['provider']='sofascore';atomic_json(raw,saved)
            with self.assertRaisesRegex(ValueError,'Invalid legacy'):migrate_legacy(root,quiet)
            self.assertTrue(raw.exists());self.assertFalse((target/'raw').exists())

    def test_interrupted_copy_can_resume_without_removing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root)
            with patch('collection.ensemble.atomic_copy',side_effect=OSError('Interrupted')):
                with self.assertRaises(OSError):migrate_legacy(root,quiet)
            self.assertTrue(old.exists());self.assertEqual(migrate_legacy(root,quiet),1)
            self.assertTrue((target/'raw/whoscored').exists())

    def test_new_file_during_migration_retains_source_until_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root)
            def copy(source,destination):
                atomic_copy(source,destination);(old/'late-note.txt').write_text('keep me')
            with patch('collection.ensemble.atomic_copy',side_effect=copy):
                with self.assertRaisesRegex(OSError,'changed during migration'):migrate_legacy(root,quiet)
            self.assertTrue((old/'late-note.txt').exists())
            self.assertEqual(migrate_legacy(root,quiet),1)
            self.assertEqual((target/'whoscored_late-note.txt').read_text(),'keep me')

    def test_destination_symbolic_link_is_rejected_without_removing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,target=legacy(root);outside=Path(tmp)/'outside';outside.mkdir()
            target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(outside,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'destination leaves'):migrate_legacy(root,quiet)
            self.assertTrue(old.exists());self.assertEqual(list(outside.iterdir()),[])

    def test_transfer_of_whoscored_alias_preserves_source_worker_origin(self):
        for prefixed in (False,True):
            with self.subTest(prefixed=prefixed),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);source=base/'ensemble/msi/data';legacy(source,prefixed=prefixed)
                atomic_json(system_path(source,"identity.json"),{'worker_id':'msi','instance_id':'source-instance'})
                import_pending(base/'data',base/'ensemble',quiet)
                with ensemble_session(base/'data',base/'ensemble',quiet,worker_id='mac') as progress:
                    record=progress.local_records[job_key('whoscored_events','Premier League','2024/25',11)]
                    self.assertEqual(record['origins'],['msi'])
                self.assertFalse(source.exists());self.assertFalse((base/'data/whoscored').exists())

    def test_explicit_migration_refreshes_inventory_and_respects_active_scraper_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);old,target=legacy(base/'data')
            args=['--data-root',str(base/'data'),'--ensemble-root',str(base/'ensemble')]
            with FileLock(str(base/'data/_system/locks/collector.lock'),timeout=0):
                self.assertEqual(migrate(args),2)
            self.assertTrue(old.exists())
            self.assertEqual(migrate(args),0)
            inventories=list((base/'ensemble/workers').glob('*.json'))
            self.assertEqual(len(inventories),1)
            self.assertEqual(len(json.loads(inventories[0].read_text())['records']),1)

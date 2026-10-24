from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import errno
import shutil
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
        # These tiny fixtures model a card with available space, independently
        # of the developer's disk. Low-space cases override this explicitly.
        usage=shutil.disk_usage(self.base)
        available=type(usage)(max(usage.total,64*1024**3),usage.used,32*1024**3)
        capacity=patch('tools.prepare_transfer.shutil.disk_usage',return_value=available)
        capacity.start();self.addCleanup(capacity.stop)
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
    def test_background_verification_reports_progress_and_can_stop_without_removing_source(self):
        from collection.backgroundImports import import_staged
        from collection.maintenance import DeferredWork
        import threading
        prepare(self.root,self.bundle,quiet)
        stop=threading.Event();updates=[]
        live=self.base/'msi';atomic_json(live/'.soccer-scraper.json',{'format':1})
        def progress(status):
            updates.append(status)
            if status['state']=='verifying_final':stop.set()
        with self.assertRaises(DeferredWork):
            import_staged(live,self.bundle.parent,stop=stop,report=quiet,on_progress=progress)
        self.assertTrue(self.bundle.exists())
        first=[s for s in updates if s['state']=='verifying']
        self.assertEqual(first[0]['verified'],0)
        self.assertEqual(first[-1]['verified'],first[-1]['total'])
        self.assertTrue(any(s['state']=='importing' for s in updates))
        # Retry reuses committed parts and only then removes verified staging.
        seasons=[]
        self.assertTrue(import_staged(live,self.bundle.parent,report=quiet,on_season=seasons.append))
        self.assertFalse(self.bundle.exists());self.assertIn('premier-league/2024-2025',seasons)
        self.assertTrue((self.root/'.soccer-scraper.json').exists())

    def test_publication_queue_failure_retains_verified_portable_source_for_retry(self):
        from collection.backgroundImports import import_staged
        prepare(self.root,self.bundle,quiet)
        live=self.base/'msi';atomic_json(live/'.soccer-scraper.json',{'format':1})
        def fail(season):raise RuntimeError('Publication queue unavailable')
        with self.assertRaisesRegex(RuntimeError,'queue unavailable'):
            import_staged(live,self.bundle.parent,report=quiet,on_season=fail)
        self.assertTrue(self.bundle.exists())
        queued=[]
        self.assertTrue(import_staged(live,self.bundle.parent,report=quiet,on_season=queued.append))
        self.assertFalse(self.bundle.exists());self.assertTrue(queued)

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

    def test_chunked_export_resumes_without_recopying_prior_raw_data_and_keeps_scraping_unlocked(self):
        from filelock import FileLock
        from storage.layout import system_path
        raw=self.root/'premier-league/2024-2025/raw/sofascore'
        atomic_json(raw/'extra-a.json',{'payload':'a'*100000})
        atomic_json(raw/'extra-b.json',{'payload':'b'*100000})
        with FileLock(str(system_path(self.root,'.collector.lock'))):
            prepare(self.root,self.bundle,quiet,session='card',max_bytes=130000)
        first=verify_bundle(self.bundle)
        self.assertTrue(first['partial'])
        self.assertLessEqual(sum(row['size'] for row in first['files']),130000)
        second_folder=self.bundle.with_name('main-data-02')
        prepare(self.root,second_folder,quiet,session='card',max_bytes=130000)
        second=verify_bundle(second_folder)
        self.assertFalse(second['partial'])
        first_raw={row['path'] for row in first['files'] if '/raw/' in row['path']}
        second_raw={row['path'] for row in second['files'] if '/raw/' in row['path']}
        self.assertFalse(first_raw & second_raw)
        self.assertIn('premier-league/2024-2025/raw/sofascore/extra-b.json',first_raw|second_raw)
        self.assertTrue((raw/'extra-a.json').is_file())
        with self.assertRaisesRegex(ValueError,'already exported'):
            prepare(self.root,self.bundle.with_name('main-data-03'),quiet,session='card',max_bytes=130000)

    def test_card_allocation_budget_rounds_tiny_files_and_directory_overhead(self):
        from tools.prepare_transfer import Footprint,RESERVE
        for index in range(40):
            atomic_json(self.root/f'premier-league/2024-2025/raw/sofascore/extra-{index}.json',{'small':index})
        oracle=self.bundle.with_name('oracle')
        prepare(self.root,oracle,quiet)
        rows={row['path']:row for row in verify_bundle(oracle)['files']}
        unit=131072;full=Footprint(rows,unit);full.add(rows)
        self.assertGreater(full.allocated,full.payload*3)
        budget=full.allocated-10*unit
        usage=shutil.disk_usage(self.bundle.parent)
        disk_usage=shutil.disk_usage
        def card_usage(path):
            if Path(path).resolve()==self.bundle.parent.resolve():return type(usage)(usage.total,usage.used,RESERVE+budget)
            return disk_usage(path)
        with patch('tools.prepare_transfer.allocation_unit',return_value=unit), \
             patch('tools.prepare_transfer.shutil.disk_usage',side_effect=card_usage):
            prepare(self.root,self.bundle,quiet,session='allocated')
        manifest=verify_bundle(self.bundle)
        selected={row['path']:row for row in manifest['files']}
        actual=Footprint(selected,unit);actual.add(selected)
        self.assertLessEqual(actual.allocated,budget)
        self.assertTrue(manifest['partial'])
        self.assertLess(len(selected),len(rows))

    def test_interrupted_copy_reuses_verified_files_and_pins_retired_source_generations(self):
        from tools.prepare_transfer import export_paths
        original=shutil.copyfile;copied=[]
        def interrupt(source,target,*args,**kwargs):
            if Path(target).name=='copy.part':
                if len(copied)==7:
                    Path(target).write_bytes(b'partial')
                    raise OSError(errno.ENOSPC,'simulated full card')
                copied.append(Path(source))
            return original(source,target,*args,**kwargs)
        with patch('tools.prepare_transfer.shutil.copyfile',side_effect=interrupt):
            with self.assertRaisesRegex(OSError,'retained'):
                prepare(self.root,self.bundle,quiet,session='resume')
        local,incomplete=export_paths(self.root.resolve(),self.bundle.resolve())
        self.assertTrue((local/'plan.json').exists());self.assertTrue(incomplete.exists())
        self.assertFalse((incomplete/'copy.part').exists());self.assertFalse(self.bundle.exists())
        self.assertFalse((self.root/'_system/storage/portable-exports/resume.json').exists())
        live=PandasStore(self.root/'premier-league/2024-2025/match_checkpoints/1')
        retired=live.current_directory
        dataset=live.from_csv();dataset.matches[1].metadata['later_scrape']=True;live.to_csv(dataset)
        self.assertFalse(retired.exists())
        repeated=[]
        def record(source,target,*args,**kwargs):
            if Path(target).name=='copy.part':repeated.append(Path(source))
            return original(source,target,*args,**kwargs)
        with patch('tools.prepare_transfer.season_files',side_effect=AssertionError('Resnapshot instead of resuming')), \
             patch('tools.prepare_transfer.shutil.copyfile',side_effect=record):
            prepare(self.root,self.bundle,quiet,session='resume')
        self.assertFalse(set(copied)&set(repeated))
        verify_bundle(self.bundle)
        exported=PandasStore(self.bundle/'premier-league/2024-2025/match_checkpoints/1').from_csv()
        self.assertNotIn('later_scrape',exported.matches[1].metadata)
        self.assertTrue(live.from_csv().matches[1].metadata['later_scrape'])
        self.assertFalse(incomplete.exists());self.assertFalse(local.exists())

    def test_receipt_failure_after_publish_is_recoverable_without_recopied_files(self):
        from tools.prepare_transfer import atomic_json as original
        receipt=self.root/'_system/storage/portable-exports/receipt.json'
        def fail(path,*args,**kwargs):
            if Path(path).resolve()==receipt.resolve():raise OSError('simulated receipt write failure')
            return original(path,*args,**kwargs)
        with patch('tools.prepare_transfer.atomic_json',side_effect=fail):
            with self.assertRaisesRegex(OSError,'receipt write failure'):
                prepare(self.root,self.bundle,quiet,session='receipt')
        verify_bundle(self.bundle)
        with patch('tools.prepare_transfer.shutil.copyfile',side_effect=AssertionError('Already published bundle recopied')):
            prepare(self.root,self.bundle,quiet,session='receipt')
        self.assertTrue(receipt.is_file())

    def test_corrupted_previously_copied_file_is_repaired_on_resume(self):
        from tools.prepare_transfer import export_paths
        original=shutil.copyfile;count=[0]
        def interrupt(source,target,*args,**kwargs):
            if Path(target).name=='copy.part':
                count[0]+=1
                if count[0]==4:raise OSError(errno.ENOSPC,'card full')
            return original(source,target,*args,**kwargs)
        with patch('tools.prepare_transfer.shutil.copyfile',side_effect=interrupt):
            with self.assertRaises(OSError):prepare(self.root,self.bundle,quiet,session='repair')
        _,incomplete=export_paths(self.root.resolve(),self.bundle.resolve())
        target=next(path for path in (incomplete/'data').rglob('*') if path.is_file())
        target.write_bytes(b'x'*target.stat().st_size)
        prepare(self.root,self.bundle,quiet,session='repair')
        verify_bundle(self.bundle)

    def test_old_plan_resumes_with_eight_gib_reserve_without_recopying_saved_data(self):
        from tools.prepare_transfer import export_paths,RESERVE
        self.assertEqual(RESERVE,8*1024**3)
        original=shutil.copyfile;count=[0]
        def interrupt(source,target,*args,**kwargs):
            if Path(target).name=='copy.part':
                count[0]+=1
                if count[0]==4:raise OSError(errno.ENOSPC,'card full')
            return original(source,target,*args,**kwargs)
        with patch('tools.prepare_transfer.shutil.copyfile',side_effect=interrupt):
            with self.assertRaises(OSError):prepare(self.root,self.bundle,quiet,session='old-reserve')
        local,incomplete=export_paths(self.root.resolve(),self.bundle.resolve())
        import json
        plan=json.loads((local/'plan.json').read_text());plan['reserve']=512*1024**2
        atomic_json(local/'plan.json',plan)
        usage=shutil.disk_usage(self.bundle.parent);real_usage=shutil.disk_usage
        def card_usage(path):
            if Path(path).resolve()==self.bundle.parent.resolve():return type(usage)(usage.total,usage.used,RESERVE-1)
            return real_usage(path)
        with patch('tools.prepare_transfer.shutil.disk_usage',side_effect=card_usage), \
             patch('tools.prepare_transfer.shutil.copyfile',side_effect=AssertionError('Copied below reserve')):
            with self.assertRaisesRegex(OSError,'keep 8 GiB free'):
                prepare(self.root,self.bundle,quiet,session='old-reserve')
        self.assertTrue(incomplete.is_dir());self.assertTrue((local/'source').is_dir())
        self.assertFalse((self.root/'_system/storage/portable-exports/old-reserve.json').exists())
        saved=next(path for path in (incomplete/'data').rglob('*') if path.is_file())
        companion=saved.with_name('._'+saved.name);companion.write_bytes(b'\x00\x05\x16\x07metadata')
        def recovered_usage(path):
            if Path(path).resolve()==self.bundle.parent.resolve():
                free=RESERVE-1 if companion.exists() else RESERVE+64*1024**2
                return type(usage)(usage.total,usage.used,free)
            return real_usage(path)
        with patch('tools.prepare_transfer.shutil.disk_usage',side_effect=recovered_usage):
            prepare(self.root,self.bundle,quiet,session='old-reserve')
        verify_bundle(self.bundle)

    def test_metadata_cleanup_is_restricted_to_planned_companions_with_appledouble_header(self):
        from tools.prepare_transfer import clean_transfer_metadata
        stage=self.bundle/'data';folder=stage/'league/2024-2025/raw/provider';folder.mkdir(parents=True)
        data=folder/'match.json';data.write_bytes(b'football data')
        companion=folder/'._match.json';companion.write_bytes(b'\x00\x05\x16\x07metadata')
        directory_companion=folder.parent/'._provider';directory_companion.write_bytes(b'\x00\x05\x16\x07metadata')
        unrecognized=folder/'._other.json';unrecognized.write_bytes(b'\x00\x05\x16\x07metadata')
        ordinary=folder/'._plain.json';ordinary.write_bytes(b'not AppleDouble')
        outside=self.bundle.parent/'._unrelated';outside.write_bytes(b'\x00\x05\x16\x07metadata')
        rows=[{'path':'league/2024-2025/raw/provider/match.json'},{'path':'league/2024-2025/raw/provider/plain.json'}]
        self.assertEqual(clean_transfer_metadata(stage,rows),2)
        self.assertEqual(data.read_bytes(),b'football data')
        self.assertFalse(companion.exists());self.assertFalse(directory_companion.exists())
        self.assertTrue(unrecognized.exists());self.assertTrue(ordinary.exists());self.assertTrue(outside.exists())

    def test_new_copies_remove_metadata_sidecars_as_they_go(self):
        original=shutil.copyfile
        def copy_with_metadata(source,target,*args,**kwargs):
            result=original(source,target,*args,**kwargs)
            if Path(target).name=='copy.part':
                target=Path(target);target.with_name('._'+target.name).write_bytes(b'\x00\x05\x16\x07metadata')
            return result
        from tools.prepare_transfer import remove_metadata_sidecar as clean
        removals=[]
        def record(path):
            result=clean(path)
            if result:removals.append(path)
            return result
        with patch('tools.prepare_transfer.shutil.copyfile',side_effect=copy_with_metadata), \
             patch('tools.prepare_transfer.remove_metadata_sidecar',side_effect=record):
            prepare(self.root,self.bundle,quiet)
        verify_bundle(self.bundle)
        self.assertTrue(removals)
        self.assertFalse(list(self.bundle.rglob('._*')))


if __name__=='__main__':unittest.main()

"""Concurrency and capacity regressions, rather than just background API wiring."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from filelock import FileLock

from collection.maintenance import background,foreground,DeferredWork,LowSpace,require_space,lock_paths
from collection.backgroundImports import import_parts,import_staged,snapshot_data
from collection.storageAgent import StorageAgent,season_files
from collection.ensemble import ensemble_session
from collection.ensembleProgress import PortableProgress,read_progress
from collection.inventoryMonitor import InventoryMonitor
from collection.workerIdentity import ensure_identity,save_origins,read_origins
from storage.pandasStore import PandasStore,atomic_json
from tests.test_ensemble import populate,quiet


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.source=self.base/'headless'
        self.season='premier-league/2024-2025';self.ensemble=self.base/'ensemble'
        atomic_json(self.root/'.soccer-scraper.json',{'format':1});ensure_identity(self.root,'msi')

    def test_foreground_requests_make_a_running_background_reader_yield(self):
        entered=threading.Event();finished=threading.Event()
        def collect():
            with foreground(self.root,self.season):entered.set()
            finished.set()
        with background(self.root,self.season) as check:
            thread=threading.Thread(target=collect);thread.start()
            waiting=lock_paths(self.root,self.season)[1]
            for _ in range(100):
                if waiting.exists():break
                threading.Event().wait(.01)
            with self.assertRaises(DeferredWork):check()
            self.assertFalse(entered.is_set())
        self.assertTrue(finished.wait(2));thread.join(2)

    def test_scraper_poll_never_executes_slow_imports_inline(self):
        agent=StorageAgent(self.root,'msi',{'hub':'msi','consumers':['main']},dataset_root=self.base/'dataset',log=quiet)
        agent.server=object()
        with patch.object(agent,'maintenance_once',side_effect=AssertionError('Inline maintenance')):
            self.assertFalse(agent.boundary())
        agent.maintenance_changed.set();self.assertTrue(agent.boundary());self.assertFalse(agent.boundary())

    def test_startup_does_not_wait_for_inventory_or_import(self):
        with patch('collection.ensemble.import_pending',side_effect=AssertionError('Inline import')), \
             patch.object(PortableProgress,'scan',side_effect=AssertionError('Inline scan')):
            with ensemble_session(self.root,self.ensemble,quiet,worker_id='msi',background=True) as progress:
                self.assertIsNotNone(progress)

    def test_import_resumes_verified_parts_and_preserves_source_when_disk_is_low(self):
        populate(self.source,limit=1);ensure_identity(self.source,'headless')
        files=season_files(self.source,self.season)
        with patch('collection.backgroundImports.require_space',side_effect=LowSpace('disk full')):
            with self.assertRaises(LowSpace):import_parts(self.root,files,owner='headless',batch='one',report=quiet)
        self.assertFalse((self.root/self.season/'match_checkpoints/1/CURRENT.json').exists())
        self.assertTrue((self.source/self.season/'match_checkpoints/1/CURRENT.json').exists())
        import_parts(self.root,files,owner='headless',batch='one',report=quiet)
        with patch('collection.ensemble.import_folder',side_effect=AssertionError('Already acknowledged part repeated')):
            import_parts(self.root,files,owner='headless',batch='one',report=quiet)
        self.assertTrue((self.root/self.season/'match_checkpoints/1/CURRENT.json').exists())
        # The incoming checkpoint's exact bytes are retained at short, hashed paths.
        journals=list((self.root/'_system/imports').glob('*/import.json'))
        self.assertTrue(journals)
        for journal in journals:
            saved=json.loads(journal.read_text())
            for relative,sha in saved['checkpoint_objects'].items():
                self.assertTrue((journal.parent/'objects'/sha).is_file())
                self.assertLess(len(str(journal.parent/'objects'/sha)),len(str(journal.parent/'source_checkpoints'/relative)))

    def test_origin_and_inventory_writers_do_not_erase_each_other(self):
        first=PortableProgress(self.ensemble/'provenance.json',self.root,worker_id='msi')
        second=PortableProgress(self.ensemble/'provenance.json',self.root,worker_id='msi')
        first.record('sofascore','Premier League','2024/25',1)
        second.record('sofascore','La Liga','2023/24',2)
        self.assertEqual(len(read_progress(first.worker_path)['records']),2)
        save_origins(self.root,{'a':['headless']});save_origins(self.root,{'b':['msi']})
        self.assertEqual(read_origins(self.root)['a'],['headless'])
        self.assertEqual(read_origins(self.root)['b'],['msi'])

    def test_inventory_skips_busy_season_then_indexes_it_after_release(self):
        populate(self.root,limit=1)
        monitor=InventoryMonitor(self.root,self.ensemble,'msi',report=quiet)
        with foreground(self.root,self.season):
            self.assertFalse(monitor.check());self.assertGreaterEqual(monitor.status['seasons_waiting'],1)
        self.assertTrue(monitor.check());self.assertGreater(monitor.status['records'],0)

    def test_build_snapshot_survives_foreground_checkpoint_replacement(self):
        populate(self.root,limit=1)
        snapshot=self.base/'snapshot';snapshot_data(self.root,snapshot)
        live=PandasStore(self.root/self.season/'match_checkpoints/1');original=live.current_directory
        dataset=live.from_csv();dataset.matches[1].metadata['new']='foreground';live.to_csv(dataset)
        self.assertFalse(original.exists())
        pinned=PandasStore(snapshot/self.season/'match_checkpoints/1').from_csv()
        self.assertNotIn('new',pinned.matches[1].metadata)
        self.assertEqual(live.from_csv().matches[1].metadata['new'],'foreground')

    def test_low_disk_space_keeps_the_scraper_reserve(self):
        from collections import namedtuple
        usage=namedtuple('usage','total used free')
        with patch('collection.maintenance.shutil.disk_usage',return_value=usage(100,95,5)):
            with self.assertRaises(LowSpace):require_space(self.root,4,reserve=2)
            require_space(self.root,3,reserve=2)

    def test_concordance_reads_checkpoints_when_foreground_exports_are_deferred(self):
        populate(self.root,limit=1)
        directory=self.root/self.season
        # populate() makes exports for its old provider fixtures; remove them to
        # reproduce the new scrape-only foreground workflow.
        for name in ('player_match_stats.csv','identities.csv'):(directory/name).unlink(missing_ok=True)
        import shutil
        shutil.rmtree(directory/'dataset',ignore_errors=True)
        from dataset_builder import build_id_tables
        output=self.base/'dataset'
        build_id_tables(self.root,output)
        import csv
        with (output/'concordance/player_ids.csv').open() as stream:rows=list(csv.DictReader(stream))
        self.assertTrue(rows);self.assertTrue(any(row['sofascore_id'] for row in rows))

    def test_an_incomplete_drive_copy_is_not_imported_or_removed(self):
        source=self.ensemble/'copying'
        atomic_json(source/'.soccer-scraper.json',{'format':1})
        self.assertFalse(import_staged(self.root,self.ensemble,report=quiet))
        self.assertTrue(source.exists())

    def test_deferred_exports_do_not_run_the_foreground_csv_builder(self):
        import main
        with patch('main._single',return_value=0) as single:
            self.assertEqual(main._normal_main(['resume','--no-build-dataset','--defer-exports']),0)
        self.assertIn('--no-build-csvs',single.call_args.args[0])

    def test_old_local_inventory_cannot_skip_a_missing_local_checkpoint(self):
        from collection.ensembleProgress import progress_session,remote_done
        progress=PortableProgress(self.ensemble/'provenance.json',self.root,worker_id='msi')
        progress.record('sofascore','Premier League','2024/25',1,['provider:sofascore'])
        with progress_session(progress):
            self.assertFalse(remote_done('sofascore','Premier League','2024/25',1,['provider:sofascore']))

    def test_schedule_only_inventory_change_is_reported_to_main(self):
        directory=self.root/self.season
        atomic_json(directory/'job.json',{'competition':'Premier League','season':'2024/25'})
        atomic_json(directory/'schedule.json',{'events':[{'id':1}]})
        notices=[]
        monitor=InventoryMonitor(self.root,self.ensemble,'msi',report=quiet,on_change=lambda:notices.append(True))
        self.assertTrue(monitor.check())
        self.assertFalse(monitor.check())
        atomic_json(directory/'schedule.json',{'events':[{'id':1},{'id':2}]})
        self.assertTrue(monitor.check());self.assertEqual(len(notices),2)

    def test_failed_validation_withdraws_stale_holdings_before_reporting_to_main(self):
        populate(self.root,limit=1)
        notices=[]
        monitor=InventoryMonitor(self.root,self.ensemble,'msi',report=quiet,on_change=lambda:notices.append(True))
        self.assertTrue(monitor.check())
        marker=next(self.root.glob('*/*/match_checkpoints/*/CURRENT.json'))
        (PandasStore(marker.parent).current_directory/'matches.csv').write_text('invalid_columns\n1\n')
        self.assertTrue(monitor.check());self.assertEqual(monitor.status['state'],'attention')
        saved=read_progress(self.ensemble/'workers/msi.json')
        self.assertFalse(saved['records']);self.assertTrue(saved['schedules'])
        self.assertEqual(len(notices),2)


if __name__=='__main__':unittest.main()

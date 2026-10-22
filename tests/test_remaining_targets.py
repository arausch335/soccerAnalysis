from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from collection.workInstructions import remaining_scope,remaining_targets
from collection.seasonRunner import SeasonRunner
from collection.progress import observe_progress
from collection.jobCompletion import CompletionStore
from providers.transports.bridge import ExtensionBridge
from storage.pandasStore import PandasStore
from tests import test_providers as fixtures
from tests.test_job_completion import CompletionTests


class RemainingTargetTests(unittest.TestCase):
    def test_only_one_remaining_game_in_a_380_game_season_is_opened(self):
        class Sofa(fixtures.Sofa):
            def iter_season_events(self,*args):
                yield from (fixtures.event(mid) for mid in range(1,381))
        job=SimpleNamespace(competition='Premier League',as_dict=lambda:{'season':'2024-2025'})
        command={'configuration':'current','schedule_known':True,'remaining':{'380':['sofascore_stats']}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);broken=root/'match_checkpoints/1/CURRENT.json';broken.parent.mkdir(parents=True);broken.write_text('Do not open this already resolved checkpoint')
            sofa=Sofa();runner=SeasonRunner({'sofascore':sofa},root,providers=['sofascore'],spatial=(),report=lambda *a,**k:None)
            events=[]
            with remaining_scope(job,command),observe_progress(events.append),patch.object(PandasStore,'from_csv',side_effect=AssertionError('Resolved checkpoint opened')):
                result=runner.run('Premier League','2024/2025',build_csvs=False)
            self.assertEqual(result.match_id.tolist(),[380]);self.assertEqual(sofa.calls,[380])
            started=[e['match_id'] for e in events if e['event']=='match_started'];self.assertEqual(started,['380'])
            schedule=next(e for e in events if e['event']=='schedule');self.assertEqual(schedule['total'],380);self.assertEqual(schedule['remaining'],1)
        self.assertIsNone(remaining_targets('Premier League','2024-2025'))

    def test_remaining_targets_never_leak_into_another_season_or_unknown_schedule(self):
        job=SimpleNamespace(competition='Premier League',as_dict=lambda:{'season':'2024-2025'})
        with remaining_scope(job,{'configuration':'known','schedule_known':True,'remaining':{'7':[]}}):
            self.assertEqual(remaining_targets('Premier League','2024-2025'),{'7'})
            self.assertIsNone(remaining_targets('Premier League','2023-2024'))
        with remaining_scope(job,{'configuration':'known','schedule_known':False,'remaining':{}}):
            self.assertIsNone(remaining_targets('Premier League','2024-2025'))

    def test_done_snapshot_survives_new_fixtures_and_missing_inventory_until_explicit_reopen(self):
        fixture=CompletionTests();fixture.setUp()
        with tempfile.TemporaryDirectory() as root:
            store=CompletionStore(root);self.assertTrue(store.freeze(fixture.job,fixture.summary(store),0))
            fixture.schedule['match_ids'].append('2');fixture.reports[0]['inventory']['records'].clear()
            frozen=fixture.summary(store);self.assertTrue(frozen['done']);self.assertTrue(frozen['frozen']);self.assertEqual(frozen['known_matches'],1)
            store.record(fixture.job,'reopen',reason='Explicitly inspect this season again')
            fresh=fixture.summary(store);self.assertFalse(fresh['done']);self.assertEqual(fresh['known_matches'],2)

    def test_extension_restart_reuses_private_port_and_connection_token(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'extension-session.json';first=ExtensionBridge(report=lambda *a,**k:None,session_path=path);first.start()
            address,token=first.address,first.token;first.close()
            second=ExtensionBridge(report=lambda *a,**k:None,session_path=path)
            try:
                second.start();self.assertEqual(second.address,address);self.assertEqual(second.token,token)
            finally:second.close()
            import os
            if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_reopen_requires_an_explicit_worker_check_before_refreezing_complete_data(self):
        fixture=CompletionTests();fixture.setUp()
        with tempfile.TemporaryDirectory() as root:
            store=CompletionStore(root);store.freeze(fixture.job,fixture.summary(store),0)
            store.record(fixture.job,'reopen',reason='Inspect this completed season again')
            opened=fixture.summary(store);self.assertFalse(opened['done']);self.assertTrue(opened['audit_requested'])
            with remaining_scope(fixture.job,opened):self.assertEqual(remaining_targets(fixture.job.competition,fixture.season),{'1'})
            fixture.reports[0]['runtime']['jobs'][fixture.job.id]['review_audit_id']=opened['audit_id']
            checked=fixture.summary(store);self.assertTrue(checked['done']);self.assertFalse(checked['audit_requested'])

    def test_inventory_monitor_does_not_open_a_frozen_season(self):
        from unittest.mock import MagicMock
        from collection.inventoryMonitor import InventoryMonitor
        from storage.pandasStore import atomic_json
        fixture=CompletionTests();fixture.setUp()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';ensemble=Path(tmp)/'ensemble'
            atomic_json(root/'premier-league'/fixture.season/'job.json',{})
            completed=CompletionStore(root);completed.freeze(fixture.job,fixture.summary(completed),0)
            progress=MagicMock();progress.worker_path=ensemble/'workers/main.json';progress.scan_summary={'reused':0,'validated':0}
            with patch('collection.inventoryMonitor.PortableProgress',return_value=progress):
                InventoryMonitor(root,ensemble,'main',report=lambda *a,**k:None).check()
            progress.scan.assert_not_called()

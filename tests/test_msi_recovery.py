import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd
from collection.storageServer import Archive
from dataset_pipeline.build import package_collection
from tools.audit_integration import inspect_queues,saved_errors,audit,EXPECTED,now
from tools.review_imports import retry_builds


class MSIRecoveryTests(unittest.TestCase):
    def test_failed_attempts_do_not_claim_missing_collected_data_but_success_elsewhere_does(self):
        from collection.workerIdentity import ensure_identity
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';root.mkdir();ensure_identity(root,'msi');Archive(root)
            complete={'capabilities':['provider:sofascore'],'resources':{'sofascore_stats':'complete'}}
            failed={'capabilities':[],'resources':{'sofascore_stats':'failed'}}
            worker={'worker':'main','last_seen':now().isoformat(),'inventory_updated_at':now().isoformat(),
                'runtime_updated_at':now().isoformat(),'records':{'good':complete,'failed':failed}}
            expected=Path(tmp)/'expected.json'
            def check(workers):
                expected.write_text(json.dumps({'format':EXPECTED,'generated_at':now().isoformat(),'workers':workers}))
                return audit(root,root/'ensemble',expected,scan=lambda r:{'good':complete})
            result=check([worker]);self.assertEqual(result['state'],'ready')
            self.assertEqual(result['missing'],[]);self.assertEqual(result['expected_attempt_only_records'],1)
            peer={**worker,'worker':'headless','records':{'failed':complete}}
            result=check([worker,peer]);self.assertEqual(result['state'],'not_ready')
            self.assertEqual(result['missing'][0]['key'],'failed')
            result=check([{**worker,'records':{'failed':failed}}])
            self.assertEqual(result['state'],'not_ready');self.assertEqual(result['expected_resource_records'],0)

    def test_transient_windows_directory_lock_is_retried_and_permanent_lock_rolls_back(self):
        for permanent in (False,True):
            with self.subTest(permanent=permanent),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'raw';source.mkdir()
                (source/'config.json').write_text(json.dumps({'competition':'Bundesliga','season':'2026/2027'}))
                (source/'schedule.json').write_text('{}')
                destination=root/'dataset/bundesliga/2026-2027';destination.mkdir(parents=True)
                (destination/'fixtures.csv').write_text('previous export')
                original=os.replace;attempts=[]
                def locked(src,dst):
                    if str(src).endswith('.staging'):
                        attempts.append(str(src))
                        if permanent or len(attempts)==1:
                            exc=PermissionError('Sharing conflict');exc.winerror=5;raise exc
                    return original(src,dst)
                with patch('storage.pandasStore.os.replace',side_effect=locked),patch('storage.pandasStore.time.sleep'),patch('dataset_pipeline.build._fixtures',return_value=pd.DataFrame([{'match_id':1}])):
                    if permanent:
                        with self.assertRaises(PermissionError):package_collection(source,root/'dataset',replace=True,tables=('fixtures',))
                        self.assertEqual((destination/'fixtures.csv').read_text(),'previous export')
                    else:
                        package_collection(source,root/'dataset',replace=True,tables=('fixtures',))
                        self.assertEqual(len(attempts),2)
                        self.assertEqual(pd.read_csv(destination/'fixtures.csv').match_id.tolist(),[1])
                self.assertFalse(list(destination.parent.glob('*.staging')))
                self.assertFalse(list(destination.parent.glob('*.backup')))

    def test_retry_only_blocked_exports_with_saved_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);job=root/'bundesliga/2026-2027/job.json';job.parent.mkdir(parents=True);job.write_text('{}')
            archive=Mock();archive.diagnostics.return_value={'builds':[
                {'season':'bundesliga/2026-2027','state':'blocked'},
                {'season':'bundesliga/2019-2020','state':'blocked'},
                {'season':'bundesliga/2018-2019','state':'removed'},
                {'season':'premier-league/2024-2025','state':'pending'}]}
            result=retry_builds(root,archive)
            self.assertEqual(result['retried_builds'],['bundesliga/2026-2027'])
            self.assertEqual(result['still_blocked'],['bundesliga/2019-2020'])
            archive.schedule_build.assert_called_once_with('bundesliga/2026-2027')

    def test_audit_reports_pending_and_blocked_exports_even_with_no_raw_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root)
            archive.schedule_build('bundesliga/2026-2027')
            archive.schedule_build('serie-a/2024-2025')
            archive.block_build('bundesliga/2026-2027',1,ValueError('Review required'))
            counts,problems=inspect_queues(root,root/'ensemble')
            self.assertEqual((counts['pending_builds'],counts['blocked_builds']),(1,1))
            self.assertTrue(any('pending' in p for p in problems))
            self.assertTrue(any('set aside' in p for p in problems))

    def test_audit_keeps_waiting_input_builds_visible_as_unfinished_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root)
            archive.schedule_build('bundesliga/2019-2020');archive.refresh_build_inputs()
            counts,problems=inspect_queues(root,root/'ensemble')
            self.assertEqual(counts['waiting_input_builds'],1)
            self.assertTrue(any('waiting for saved collection inputs' in p for p in problems))

    def test_local_error_report_includes_attempts_and_discovery_but_no_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);season=root/'champions-league-qualifiers/2021-2022';season.mkdir(parents=True)
            with (season/'collection.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=['match_id','status','error','raw_payload'])
                writer.writeheader();writer.writerow(dict(match_id=1,status='partial',error='WhoScored stage discovery failed',raw_payload='must not export'))
                writer.writerow(dict(match_id=2,status='complete',error=''))
            (season/'provider_discovery.json').write_text(json.dumps({'errors':{'whoscored':'No stage'},'counts':{'whoscored':0}}))
            result=saved_errors(root)
            self.assertEqual(len(result['collection_attempts']),1)
            self.assertEqual(result['collection_attempts'][0]['match_id'],'1')
            self.assertNotIn('raw_payload',result['collection_attempts'][0])
            self.assertEqual(result['provider_discovery'][0]['errors'],{'whoscored':'No stage'})

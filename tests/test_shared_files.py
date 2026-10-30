import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage.sharedFiles import read_json,SharedFileUnavailable
from storage.pandasStore import atomic_json


class SharedFileTests(unittest.TestCase):
    def test_windows_reads_reopen_the_file_and_observe_new_instructions(self):
        for code in (5,32,33,None):
            with self.subTest(winerror=code),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'main-instructions.json';atomic_json(path,{'revision':'old'})
                original=Path.read_text;attempts=[]
                error=PermissionError(13,'File access denied')
                if code is not None:error.winerror=code
                def read(target,*args,**kwargs):
                    attempts.append(target)
                    if len(attempts)<3:
                        if len(attempts)==2:atomic_json(path,{'revision':'new'})
                        raise error
                    return original(target,*args,**kwargs)
                with patch('storage.sharedFiles.IS_WINDOWS',True),patch.object(Path,'read_text',read), \
                     patch('storage.sharedFiles.time.sleep') as sleep:
                    self.assertEqual(read_json(path),{'revision':'new'})
                self.assertEqual(len(attempts),3);self.assertEqual(sleep.call_count,2)

    def test_exhausted_read_is_bounded_and_never_substitutes_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'provenance.json';atomic_json(path,{'records':{'kept':['headless']}})
            before=path.read_bytes();error=PermissionError(13,'Denied');error.winerror=32
            with patch.object(Path,'read_text',side_effect=error) as read,patch('storage.sharedFiles.time.sleep') as sleep:
                with self.assertRaises(SharedFileUnavailable) as caught:read_json(path,missing={})
            self.assertEqual(read.call_count,12)
            self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list),2.1)
            self.assertEqual(caught.exception.filename,str(path));self.assertEqual(path.read_bytes(),before)

    def test_missing_and_malformed_files_are_distinct_from_access_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json'
            self.assertEqual(read_json(path,missing={}),{})
            with self.assertRaises(FileNotFoundError):read_json(path)
            path.write_text('{invalid')
            with patch('storage.sharedFiles.time.sleep') as sleep:
                with self.assertRaises(json.JSONDecodeError):read_json(path,missing={})
                sleep.assert_not_called()

    def test_locked_origin_registry_is_preserved_then_merged_after_recovery(self):
        from collection.workerIdentity import save_origins,read_origins
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);save_origins(root,{'old':['headless']})
            path=root/'_system/provenance.json';before=path.read_bytes()
            original=Path.read_text;error=PermissionError(13,'Locked');error.winerror=5
            def blocked(target,*args,**kwargs):
                if target==path:raise error
                return original(target,*args,**kwargs)
            with patch.object(Path,'read_text',blocked),patch('storage.sharedFiles.time.sleep'):
                with self.assertRaises(SharedFileUnavailable):save_origins(root,{'new':['msi']})
            self.assertEqual(path.read_bytes(),before)
            save_origins(root,{'new':['msi']})
            self.assertEqual(read_origins(root),{'old':['headless'],'new':['msi']})

    def test_unreadable_approval_cannot_authorize_a_skip(self):
        from types import SimpleNamespace
        from collection.workInstructions import install,approved_by_main,current_commands,requested_again,FORMAT
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);install(root,{'format':FORMAT,'worker_id':'msi','plan_revision':'plan',
                'jobs':{'one':{'recheck':True,'revision':'new'}},
                'approved':{'match':{'holders':['headless'],'capabilities':['stats']}}})
            error=PermissionError(13,'Locked');error.winerror=32
            with patch.object(Path,'read_text',side_effect=error),patch('storage.sharedFiles.time.sleep'):
                with self.assertRaises(SharedFileUnavailable):
                    approved_by_main(SimpleNamespace(root=root,origin='msi',plan_revision='plan'),'match',['stats'])
                with self.assertRaises(SharedFileUnavailable):current_commands(root,'msi','plan')
                with self.assertRaises(SharedFileUnavailable):requested_again(root,'msi','plan',{})

    def test_runtime_retries_identical_payload_after_a_failed_public_write(self):
        from collection.workerRuntime import Runtime
        from storage.sharedFiles import write_json
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Runtime(Path(tmp)/'data',Path(tmp)/'ensemble','msi')
            runtime.save(state='idle')
            before=runtime.public.read_bytes()
            def blocked(path,value,**kwargs):
                if path==runtime.public:raise SharedFileUnavailable(13,'Denied',str(path))
                write_json(path,value,**kwargs)
            with patch('collection.workerRuntime.atomic_json',side_effect=blocked):
                with self.assertRaises(SharedFileUnavailable):runtime.save(state='scraping')
            self.assertEqual(runtime.public.read_bytes(),before)
            runtime.save(state='scraping')
            self.assertEqual(json.loads(runtime.public.read_text())['state'],'scraping')

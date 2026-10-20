from tests.paths import system_path
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from storage.pandasStore import atomic_json
from collection.files import atomic_csv


class AtomicReplaceTests(unittest.TestCase):
    def test_transient_windows_errors_retry_without_removing_previous_json(self):
        for code in (5,32,33):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as folder:
                target=system_path(Path(folder),".ensemble-origins.json");target.write_text('{"old":true}')
                error=PermissionError('Windows access/sharing conflict');error.winerror=code
                original=os.replace;attempts=[]
                def replace(source,destination):
                    attempts.append(source)
                    self.assertEqual(json.loads(target.read_text()),{'old':True})
                    self.assertTrue(Path(source).exists())
                    if len(attempts)<3:raise error
                    original(source,destination)
                with patch('storage.pandasStore.os.replace',side_effect=replace),patch('storage.pandasStore.time.sleep') as sleep:
                    atomic_json(target,{'new':True})
                self.assertEqual(len(attempts),3);self.assertEqual(sleep.call_count,2)
                self.assertEqual(json.loads(target.read_text()),{'new':True})
                self.assertEqual(list(Path(folder).glob('*.tmp')),[])

    def test_permanent_windows_failure_preserves_previous_data_and_stops_retrying(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'checkpoint.json';target.write_text('{"saved":true}')
            error=PermissionError('Persistent access denied');error.winerror=5
            with patch('storage.pandasStore.os.replace',side_effect=error) as replace,patch('storage.pandasStore.time.sleep') as sleep:
                with self.assertRaises(PermissionError):atomic_json(target,{'saved':False})
            self.assertEqual(replace.call_count,12);self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list),2.1)
            self.assertEqual(json.loads(target.read_text()),{'saved':True})
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])

    def test_non_windows_permission_failure_propagates_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'checkpoint.json';target.write_text('{"saved":true}')
            with patch('storage.pandasStore.os.replace',side_effect=PermissionError('Denied')) as replace,patch('storage.pandasStore.time.sleep') as sleep:
                with self.assertRaises(PermissionError):atomic_json(target,{'saved':False})
            self.assertEqual(replace.call_count,1);sleep.assert_not_called()
            self.assertEqual(json.loads(target.read_text()),{'saved':True})

    def test_csv_checkpoints_also_retry_transient_windows_replacement_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'collection.csv';target.write_text('id\n1\n')
            error=PermissionError('File briefly busy');error.winerror=32
            original=os.replace;calls=[]
            def replace(source,destination):
                calls.append(source)
                if len(calls)==1:raise error
                original(source,destination)
            with patch('storage.pandasStore.os.replace',side_effect=replace),patch('storage.pandasStore.time.sleep'):
                atomic_csv(pd.DataFrame([{'id':2}]),target)
            self.assertEqual(pd.read_csv(target).id.tolist(),[2]);self.assertEqual(len(calls),2)

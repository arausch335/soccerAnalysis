from pathlib import Path
import tempfile
import os
from pathlib import PureWindowsPath
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import config
from collection import datasetIndexCache as cache
from concordance import package_state as tracking


class DatasetIndexCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_reporting_and_assignment_changes_do_not_invalidate_index_rules(self):
        (self.root/'collection').mkdir();(self.root/'config').mkdir()
        selection=self.root/'collection/selection.py';selection.write_text('# identity selection\n')
        runtime=self.root/'collection/reporting.py';runtime.write_text('# reporting\n')
        assignments=self.root/'config/workers.yaml';assignments.write_text('workers: []\n')
        with patch.object(config,'PROJECT_ROOT',self.root):
            before=cache.recipe()
            runtime.write_text('# changed reporting\n');assignments.write_text('workers: [msi]\n')
            self.assertEqual(cache.recipe(),before)
            selection.write_text('# changed identity selection\n')
            self.assertNotEqual(cache.recipe(),before)

    def test_output_format_change_does_not_invalidate_index_rules(self):
        (self.root/'config').mkdir();path=self.root/'config/settings.yaml'
        with patch.object(config,'PROJECT_ROOT',self.root):
            path.write_text('dataset:\n  concordance_format: parquet\n');before=cache.recipe()
            path.write_text('dataset:\n  concordance_format: csv\n')
            self.assertEqual(cache.recipe(),before)

    def test_temporary_snapshot_maps_to_the_original_data_cache(self):
        origin=self.root/'data';snapshot=self.root/'temporary-input';origin.mkdir();snapshot.mkdir()
        expected=cache.cache_path(origin,origin/'premier-league/2024-2025','signature')
        with tracking.snapshot_inputs(origin,snapshot):
            self.assertEqual(cache.cache_path(snapshot,snapshot/'premier-league/2024-2025','signature'),expected)


class WindowsCachePathTests(unittest.TestCase):
    def test_cache_origin_uses_long_path_normalization_before_hash_subdirectories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'data'
            normalized=Path(temporary)/'normalized'
            with patch('collection.datasetIndexCache.full_path',return_value=normalized) as normalize:
                path=cache.cache_path(root,root/'serie-a/2025-2026','a'*64)
            normalize.assert_called_once_with(root.resolve())
            self.assertEqual(path.parent.parent,normalized/'_system/cache/dataset-ids')

    def test_windows_drive_and_network_paths_keep_extended_prefix(self):
        from storage import layout
        class WindowsPath(PureWindowsPath):
            def expanduser(self):return self
            def resolve(self):return self
        for original,expected in [
                (r'C:\Users\Alexander\data',r'\\?\C:\Users\Alexander\data'),
                (r'\\server\share\data',r'\\?\UNC\server\share\data'),
                (r'\\?\C:\Users\Alexander\data',r'\\?\C:\Users\Alexander\data')]:
            with self.subTest(original=original),patch.object(layout,'Path',WindowsPath),patch.object(layout,'os',SimpleNamespace(name='nt')):
                self.assertEqual(str(layout.full_path(original)),expected)

    def test_deep_cache_atomic_write_and_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/('nested-project-'*8)/'data'
            source=root/'serie-a/2025-2026';signature='a'*64
            path=cache.cache_path(root,source,signature)
            self.assertGreater(len(str(path))+37,260)
            if os.name=='nt':self.assertTrue(str(path).startswith('\\\\?\\'))
            saved={'signature':signature,'rows':[[],[],[]]}
            cache.save(path,saved)
            self.assertEqual(cache.load(path,signature)['rows'],saved['rows'])
            self.assertFalse(list(path.parent.glob('*.tmp')))
            changed={**saved,'rows':[[[['Premier League','2024-2025','1'],{'_ids':{'sofascore_id':['1']}}]],[],[]]}
            cache.save(path,changed)
            self.assertEqual(cache.load(path,signature)['rows'],changed['rows'])

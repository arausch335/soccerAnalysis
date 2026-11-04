from pathlib import Path
import tempfile
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

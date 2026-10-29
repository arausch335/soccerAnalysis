"""The preferred batch size must never masquerade as a disk-space failure."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collection.backgroundImports import import_parts
from collection.maintenance import LowSpace


class ImportBatchCapacityTests(unittest.TestCase):
    def test_file_larger_than_batch_imports_and_receipt_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'msi';source=Path(tmp)/'source';source.mkdir()
            metadata=source/'marker.json';metadata.write_text('{}')
            data=source/'large.csv';data.write_text('match_id,player_id\n1,2\n'*30)
            files={'.soccer-scraper.json':metadata,
                   'premier-league/2024-2025/player_match_stats.csv':data}
            with patch('collection.backgroundImports.MAX_BATCH',32), \
                 patch('collection.backgroundImports.available',return_value=10**12), \
                 patch('collection.backgroundImports.require_space') as space, \
                 patch('collection.ensemble.import_folder') as integrate:
                import_parts(root,files,owner='headless',batch='large')
                self.assertEqual(integrate.call_count,1)
                self.assertGreater(space.call_args_list[0].args[1],data.stat().st_size)
                import_parts(root,files,owner='headless',batch='large')
                self.assertEqual(integrate.call_count,1)

    def test_metadata_over_batch_target_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);metadata=p/'large.json';metadata.write_text(' '*200)
            raw=p/'raw.json';raw.write_text('{}')
            files={'.soccer-scraper.json':metadata,
                   'premier-league/2024-2025/raw/fotmob/1.json':raw}
            with patch('collection.backgroundImports.MAX_BATCH',32), \
                 patch('collection.backgroundImports.available',return_value=10**12), \
                 patch('collection.backgroundImports.require_space'), \
                 patch('collection.ensemble.import_folder') as integrate:
                import_parts(p/'msi',files,owner='headless',batch='metadata')
                integrate.assert_called_once()

    def test_genuine_shortage_checks_actual_bytes_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);data=p/'raw.json';data.write_text(' '*200)
            with patch('collection.backgroundImports.available',return_value=0), \
                 patch('collection.backgroundImports.require_space',side_effect=LowSpace('real shortage')), \
                 patch('collection.ensemble.import_folder') as integrate:
                with self.assertRaisesRegex(LowSpace,'real shortage'):
                    import_parts(p/'msi',{'premier-league/2024-2025/raw/fotmob/1.json':data},owner='headless',batch='shortage')
                integrate.assert_not_called();self.assertTrue(data.exists())

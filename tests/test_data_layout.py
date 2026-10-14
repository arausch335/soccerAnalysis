from pathlib import Path
import tempfile
import unittest

from collection.paths import collection_directory, collection_name, collection_parts
from tools.migrate_data_layout import migrate, migration_plan


class CollectionPathTests(unittest.TestCase):
    def test_short_season_name_maps_to_nested_full_years(self):
        self.assertEqual(
            collection_parts('ligue-1-2024-25'),
            ('ligue-1', '2024-2025'),
        )
        path=collection_directory('/tmp/data','premier-league-2025-26')
        self.assertEqual(path,Path('/tmp/data/premier-league/2025-2026'))
        self.assertEqual(collection_name(path),'premier-league-2025-26')

    def test_invalid_or_nonconsecutive_seasons_are_rejected(self):
        for name in ('premier-league','premier-league-2024-26','-2024-25'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                collection_parts(name)

    def test_migration_moves_only_flat_season_directories_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            source=root/'premier-league-2024-25';source.mkdir()
            (source/'job.json').write_text('{}')
            cache=root/'http_cache';cache.mkdir()
            plan=migration_plan(root)
            self.assertEqual(plan,[(source,root/'premier-league'/'2024-2025')])
            migrate(root)
            self.assertTrue((root/'premier-league/2024-2025/job.json').is_file())
            self.assertTrue(cache.is_dir())
            self.assertEqual(migrate(root),[])

    def test_migration_refuses_to_merge_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            (root/'serie-a-2024-25').mkdir()
            (root/'serie-a/2024-2025').mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                migration_plan(root)


if __name__ == '__main__':
    unittest.main()

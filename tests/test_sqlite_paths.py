from contextlib import closing
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from collection.backgroundImports import snapshot_data
from collection.reporting import ReportingStore, read_logs
from providers.identityReview import ReviewStore, decisions
from storage.layout import full_path
from storage.pandasStore import atomic_json
from storage.sqlitePaths import readonly_uri
from tests.test_identity_review import proposal


class SQLitePathTests(unittest.TestCase):
    def test_windows_local_extended_and_unc_paths_keep_native_path_without_authority(self):
        for path in (r'C:\Users\name\data\review.sqlite',
                     r'\\?\C:\Users\name\data\review.sqlite',
                     r'\\server\share\data\review.sqlite',
                     r'\\?\UNC\server\share\data\review.sqlite'):
            with self.subTest(path=path):
                uri=readonly_uri(PureWindowsPath(path))
                parsed=urlsplit(uri)
                self.assertEqual(parsed.netloc,'')
                self.assertEqual(unquote(parsed.path),path)
                # The old file://%3F/... encoding fails during URI parsing,
                # before SQLite can even attempt to open a database.
                with self.assertRaises(sqlite3.OperationalError) as error:
                    sqlite3.connect(uri,uri=True)
                self.assertNotIn('authority',str(error.exception))

    def test_readonly_paths_preserve_special_characters_and_never_create_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            name='data # % é.sqlite' if os.name=='nt' else 'data ? # % é.sqlite'
            path=Path(tmp)/name
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE values_saved(value)')
                db.execute('INSERT INTO values_saved VALUES (42)');db.commit()
            with closing(sqlite3.connect(readonly_uri(path),uri=True)) as db:
                self.assertEqual(db.execute('SELECT * FROM values_saved').fetchone(),(42,))
                with self.assertRaisesRegex(sqlite3.OperationalError,'readonly'):
                    db.execute('DELETE FROM values_saved')
            missing=Path(tmp)/'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):
                sqlite3.connect(readonly_uri(missing),uri=True)
            self.assertFalse(missing.exists())

    def test_identity_reporting_and_online_backup_share_safe_path_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=full_path(Path(tmp)/'data # % é')
            atomic_json(root/'.soccer-scraper.json',{'format':1})
            reviews=ReviewStore(root);reviews.ingest([proposal()])
            reviews.decide(reviews.snapshot()['pending'][0]['id'],'confirm')
            self.assertEqual(decisions(root)[0]['target_id'],'7')
            ReportingStore(root,Path(tmp)/'ensemble')
            self.assertEqual(read_logs(root)['rows'],[])
            output=full_path(Path(tmp)/'snapshot # % é')
            snapshot_data(root,output)
            self.assertEqual(decisions(output),decisions(root))

    @unittest.skipUnless(os.name=='nt','Requires a Windows filesystem')
    def test_real_extended_windows_path_over_260_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=full_path(Path(tmp)/('nested-path-'*15)/('more-path-'*15))
            root.mkdir(parents=True)
            path=root/'review.sqlite'
            self.assertGreater(len(str(path)),260)
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE saved(value)');db.commit()
            with closing(sqlite3.connect(readonly_uri(path),uri=True)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM saved').fetchone(),(0,))


if __name__=='__main__':unittest.main()

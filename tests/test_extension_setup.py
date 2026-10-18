import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.extension_setup import ASSETS, MARKER, main, prepare_extension


class ExtensionSetupTests(unittest.TestCase):
    def test_prepared_folder_excludes_python_and_reserved_names_without_editing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir()
            actual=Path(__file__).resolve().parents[1]/'chrome_extension'
            for name in ASSETS:(source/name).write_bytes((actual/name).read_bytes())
            (source/'__init__.py').write_text('')
            (source/'__pycache__').mkdir()
            (source/'__pycache__'/'__init__.pyc').write_bytes(b'old cache')
            output=prepare_extension(root/'load-this-folder',source=source)
            self.assertEqual({p.name for p in output.iterdir()},set(ASSETS)|{MARKER})
            self.assertFalse(any(p.name.startswith('_') for p in output.rglob('*')))
            manifest=json.loads((output/'manifest.json').read_text())
            self.assertTrue((output/manifest['background']['service_worker']).is_file())
            self.assertTrue((output/manifest['action']['default_popup']).is_file())
            self.assertTrue((source/'__init__.py').exists())
            self.assertTrue((source/'__pycache__'/'__init__.pyc').exists())

    def test_rebuild_removes_stale_files_only_from_owned_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=prepare_extension(root/'prepared')
            (output/'__pycache__').mkdir()
            (output/'__pycache__'/'old.pyc').write_bytes(b'old')
            self.assertEqual(prepare_extension(output),output)
            self.assertFalse((output/'__pycache__').exists())
            unrelated=root/'unrelated';unrelated.mkdir()
            (unrelated/'keep.txt').write_text('my data')
            with self.assertRaisesRegex(ValueError,'unrelated'):prepare_extension(unrelated)
            self.assertEqual((unrelated/'keep.txt').read_text(),'my data')

    def test_source_and_its_ancestors_cannot_be_replaced(self):
        source=Path(__file__).resolve().parents[1]/'chrome_extension'
        for output in (source,source.parent,source/'build'):
            with self.subTest(output=output),self.assertRaises(ValueError):prepare_extension(output)

    def test_setup_prints_prepared_folder_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as folder,patch('pathlib.Path.cwd',return_value=Path(folder)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]),0)
            target=Path(folder)/'build'/'soccer-collector-extension'
            self.assertTrue((target/'manifest.json').exists())
            self.assertIn(str(target),output.getvalue())


if __name__=='__main__':unittest.main()

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.install_git_version import install


class InstallGitVersionTests(unittest.TestCase):
    def prepare(self, root):
        project, stage, backup = root / 'project', root / 'stage', root / 'archive'
        project.mkdir()
        (stage / '.git/info').mkdir(parents=True)
        (stage / 'providers').mkdir()
        (stage / 'dataset_builder.py').write_text('new builder')
        (stage / 'main.py').write_text('new main')
        for name in ['.venv', '.idea', 'data', 'dataset', 'outputs']:
            (project / name).mkdir()
            (project / name / 'saved').write_text(name)
        (project / 'api').mkdir()
        (project / 'api/team_id_crosswalk.json').write_text('{"teams":{}}')
        (project / 'main.py').write_text('old main')
        (project / '.env').write_text('local config')
        return project, stage, backup

    def test_preserves_environment_data_and_archives_source(self):
        with tempfile.TemporaryDirectory() as folder:
            project, stage, backup = self.prepare(Path(folder))
            before = {p: p.read_bytes() for p in project.rglob('saved')}
            install(project, stage, backup)
            self.assertEqual((project / 'main.py').read_text(), 'new main')
            self.assertEqual((backup / 'main.py').read_text(), 'old main')
            self.assertEqual((project / '.env').read_text(), 'local config')
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertEqual((project / 'providers/team_id_crosswalk.json').read_text(),
                             '{"teams":{}}')

    def test_failure_restores_original_source_and_keeps_new_clone(self):
        with tempfile.TemporaryDirectory() as folder:
            project, stage, backup = self.prepare(Path(folder))
            with patch('tools.install_git_version.shutil.copy2', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    install(project, stage, backup)
            self.assertEqual((project / 'main.py').read_text(), 'old main')
            self.assertEqual((project / 'api/team_id_crosswalk.json').read_text(), '{"teams":{}}')
            self.assertFalse((project / '.git').exists())
            self.assertEqual((stage / 'main.py').read_text(), 'new main')

    def test_preserved_folder_conflict_stops_before_archiving(self):
        with tempfile.TemporaryDirectory() as folder:
            project, stage, backup = self.prepare(Path(folder))
            (stage / 'data').mkdir()
            with self.assertRaisesRegex(ValueError, 'preserved'):
                install(project, stage, backup)
            self.assertFalse(backup.exists())
            self.assertEqual((project / 'main.py').read_text(), 'old main')

    def test_existing_git_checkout_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            project, stage, backup = self.prepare(Path(folder))
            (project / '.git').mkdir()
            with self.assertRaisesRegex(ValueError, 'already has Git'):
                install(project, stage, backup)
            self.assertFalse(backup.exists())

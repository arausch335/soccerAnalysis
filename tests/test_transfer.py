import tempfile
import unittest
from pathlib import Path

from sofascore_scrapers.locking import output_lock
from tools.include_player_project import include_project


class TransferTests(unittest.TestCase):
    def test_preserves_source_structure_and_dependencies_without_local_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, project = root / "SoccerAnalysis", root / "package"
            project.mkdir()
            for name, content in {
                "scrape.py": "from helpers.parse import parse\n",
                "helpers/parse.py": "def parse(): return 1\n",
                "requirements.txt": "requests==2.32.3\n",
                "pyproject.toml": '[project]\nname = "soccer-analysis"\n',
                ".env": "secret=local\n",
                ".venv/lib.py": "local environment\n",
                ".idea/settings.py": "local editor\n",
                "data/matches.csv": "local data\n",
                "outputs/results.jsonl": "local output\n",
            }.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            target, manifest = include_project(source, project)
            self.assertEqual({item["path"] for item in manifest},
                             {"scrape.py", "helpers/parse.py", "requirements.txt", "pyproject.toml"})
            self.assertEqual((target / "scrape.py").read_text(), (source / "scrape.py").read_text())
            self.assertTrue((source / ".env").is_file())
            self.assertFalse((target / ".env").exists())
            self.assertTrue((target / "TRANSFER_MANIFEST.json").exists())

    def test_does_not_overwrite_existing_player_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, project = root / "source", root / "project"
            source.mkdir()
            (source / "scrape.py").write_text("pass\n")
            (project / "player_match_information").mkdir(parents=True)
            with self.assertRaises(ValueError):
                include_project(source, project)

    def test_rejects_nested_source_and_empty_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                include_project(root, root / "nested")
            (root / "source").mkdir()
            with self.assertRaises(ValueError):
                include_project(root / "source", root / "project")

    def test_prevents_simultaneous_collectors_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with output_lock(folder):
                with self.assertRaises(BlockingIOError):
                    with output_lock(folder):
                        pass
            with output_lock(folder):
                pass


if __name__ == "__main__":
    unittest.main()

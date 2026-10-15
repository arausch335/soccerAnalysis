from pathlib import Path
import tempfile
import unittest

import yaml
import config


class YamlConfigTests(unittest.TestCase):
    def load_document(self, root, document):
        path = root / 'settings.yaml'
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return config.load_config(path, project_root=root)

    def document(self):
        return yaml.safe_load(config.CONFIG_FILE.read_text())

    def test_paths_and_both_workflows_come_from_one_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = self.document()
            document['paths'] = {'data': 'saved-input', 'dataset': 'ready-output'}
            document['collection'].update(action='resume', competitions=['Bundesliga'],
                                          seasons=['2024/25'], build_dataset=True)
            document['dataset']['tables']['events'] = False
            loaded = self.load_document(root, document)
            self.assertEqual(loaded['DATA_ROOT'], (root / 'saved-input').resolve())
            self.assertEqual(loaded['DATASET_ROOT'], (root / 'ready-output').resolve())
            self.assertEqual(loaded['ACTION'], 'resume')
            self.assertEqual(loaded['COMPETITIONS'], ['Bundesliga'])
            self.assertEqual(loaded['SEASONS'], ['2024/25'])
            self.assertTrue(loaded['BUILD_DATASET'])
            self.assertFalse(loaded['BUILD_EVENTS'])

    def test_typo_is_rejected_instead_of_silently_using_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            document = self.document()
            document['dataset']['tables']['event'] = document['dataset']['tables'].pop('events')
            with self.assertRaisesRegex(config.ConfigError, 'Unknown setting'):
                self.load_document(Path(folder), document)

    def test_quoted_false_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            document = self.document()
            document['collection']['build_dataset'] = 'false'
            with self.assertRaisesRegex(config.ConfigError, 'without quotes'):
                self.load_document(Path(folder), document)

    def test_empty_collection_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            document = self.document()
            document['collection']['competitions'] = []
            with self.assertRaisesRegex(config.ConfigError, 'nonempty'):
                self.load_document(Path(folder), document)

    def test_overlapping_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            document = self.document()
            document['paths']['dataset'] = 'data/dataset'
            with self.assertRaisesRegex(config.ConfigError, 'separate'):
                self.load_document(Path(folder), document)

    def test_missing_setting_and_invalid_action_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            document = self.document()
            del document['dataset']['tables']['events']
            with self.assertRaisesRegex(config.ConfigError, 'Missing setting'):
                self.load_document(Path(folder), document)
            document = self.document()
            document['collection']['action'] = 'collect-everything'
            with self.assertRaisesRegex(config.ConfigError, 'collection.action'):
                self.load_document(Path(folder), document)

    def test_duplicate_keys_and_unsafe_yaml_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.yaml'
            path.write_text('paths: {}\npaths: {}\n')
            with self.assertRaisesRegex(config.ConfigError, 'Duplicate'):
                config.load_config(path)
            path.write_text('!!python/object/apply:os.system ["echo unsafe"]')
            with self.assertRaisesRegex(config.ConfigError, 'Invalid YAML'):
                config.load_config(path)

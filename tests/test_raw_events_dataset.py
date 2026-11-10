from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collection.rawEventsDataset import package_raw_events
from providers.cache import ResponseCache


class RawEventsDatasetTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'data'
        self.root.mkdir()
        self.output = self.root.with_name('raw_events_dataset')

    def save(self, mid, *, competition='premier-league', season='2024-2025', legacy=False, events=True):
        root = self.root / 'whoscored_events' if legacy else self.root
        cache = ResponseCache(root / competition / season / 'raw')
        key = f'match/{mid}'
        cache.fetch('whoscored', key, lambda: {
            'match_id': mid,
            'matchCentreData': {
                'statusCode': 6, 'events': [{'id': mid}] if events else [],
                'home': {'teamId': 1}, 'away': {'teamId': 2},
            },
        })
        return cache.path('whoscored', key)

    def test_exports_only_selected_event_matches_with_r_layout_and_unchanged_bytes(self):
        first = self.save(123)
        self.save(123, legacy=True)
        legacy = self.save(124, legacy=True)
        self.save(125, events=False)
        self.save(126, season='2025-2026')
        self.save(127, competition='bundesliga')
        cache = ResponseCache(first.parents[1])
        cache.fetch('whoscored', 'stage/123', lambda: 42)
        cache.fetch('fotmob', 'match/123', lambda: {'events': [1]})
        (self.root / '_system').mkdir()
        (self.root / '_system' / 'worker.json').write_text('{}')

        result = package_raw_events(self.root, self.output, competitions={'premier-league'},
                                    seasons={'2024-2025'})

        self.assertEqual(result['seasons'], [
            {'competition': 'premier-league', 'season': '2024-2025', 'matches': 2},
        ])
        self.assertEqual(result['skipped_invalid_responses'], 1)
        self.assertEqual(json.loads((self.output / 'raw-events-manifest.json').read_bytes()), result)
        self.assertEqual(len(list(self.output.rglob('*.json'))), 3)
        for item, original in zip(result['files'], (first, legacy)):
            self.assertEqual(item['path'], f'premier-league/2024-2025/raw/whoscored/{original.name}')
            content = (self.output / item['path']).read_bytes()
            self.assertEqual(content, original.read_bytes())
            self.assertEqual(item['size'], len(content))
            self.assertEqual(item['sha256'], sha256(content).hexdigest())

    def test_failed_exports_leave_existing_output_and_source_intact(self):
        source = self.save(123)
        with self.assertRaisesRegex(ValueError, 'No saved'):
            package_raw_events(self.root, self.output, seasons={'2022-2023'})
        self.assertFalse(self.output.exists())
        package_raw_events(self.root, self.output)
        manifest = (self.output / 'raw-events-manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):
            package_raw_events(self.root, self.output)
        with self.assertRaisesRegex(ValueError, 'separate'):
            package_raw_events(self.root, self.root)
        self.assertEqual((self.output / 'raw-events-manifest.json').read_bytes(), manifest)
        self.assertTrue(source.is_file())

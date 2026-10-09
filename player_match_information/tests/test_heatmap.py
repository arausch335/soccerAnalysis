"""Heatmap export keeps raw points without deriving event geometry."""
import json
from pathlib import Path
import tempfile
import unittest

from collection.coordinates import extract_heatmap
from collection.jsonFiles import clear_json_cache
import test_providers


class HeatmapTests(unittest.TestCase):
    def setUp(self):
        clear_json_cache()
        self.match = test_providers.ProviderTests().baseline()
        self.player = self.match.home_lineup.players[0]

    def save(self, directory, payload, **envelope):
        Path(directory, 'heat.json').write_text(json.dumps({'payload': payload, **envelope}))
        self.match.metadata['sofascore_spatial_files'] = {'7': {'heatmap': 'heat.json'}}

    def test_preserves_coordinates_order_and_repeated_touches(self):
        points = [{'x': 12.25, 'y': 67}, {'x': 90, 'y': 10}, {'x': 90, 'y': 10}]
        with tempfile.TemporaryDirectory() as folder:
            self.save(folder, {'heatmap': points}, provider='sofascore', key='heatmap/1/7')
            result = extract_heatmap(self.match, self.player, folder)
        self.assertEqual(result['touch_count'], 3)
        self.assertEqual(json.loads(result['heatmap_xy']), points)
        self.assertEqual(set(result), {'touch_count', 'heatmap_xy'})

    def test_missing_heatmap_is_blank_but_observed_empty_map_is_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            result = extract_heatmap(self.match, self.player, folder)
            self.assertEqual(result, {'touch_count': None, 'heatmap_xy': None})
            self.save(folder, {'heatmap': []})
            self.assertEqual(extract_heatmap(self.match, self.player, folder),
                             {'touch_count': 0, 'heatmap_xy': '[]'})

    def test_average_positions_and_event_maps_are_not_used_as_touches(self):
        with tempfile.TemporaryDirectory() as folder:
            self.save(folder, {'home': [{'averageX': 50, 'averageY': 60}],
                               'passes': [{'x': 50, 'y': 60}]})
            self.assertEqual(extract_heatmap(self.match, self.player, folder),
                             {'touch_count': None, 'heatmap_xy': None})

    def test_wrong_player_cache_and_invalid_coordinates_are_rejected(self):
        for payload, envelope, error in (
                ({'heatmap': []}, {'key': 'heatmap/1/8'}, 'identity mismatch'),
                ({'heatmap': [{'x': True, 'y': 20}]}, {}, 'invalid x/y'),
                ({'heatmap': [{'x': 10}]}, {}, 'invalid x/y'),
                ({'heatmap': [{'x': float('inf'), 'y': 20}]}, {}, 'invalid x/y')):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                self.save(folder, payload, **envelope)
                with self.assertRaisesRegex(ValueError, error):
                    extract_heatmap(self.match, self.player, folder)

    def test_cache_path_cannot_escape_collection(self):
        self.match.metadata['sofascore_spatial_files'] = {'7': {'heatmap': '../other.json'}}
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, 'outside'):
            extract_heatmap(self.match, self.player, folder)

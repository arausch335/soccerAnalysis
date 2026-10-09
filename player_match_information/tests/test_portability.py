"""Cross-device UTF-8 regressions without requiring a native Windows runner."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import build_csvs
from api.safeClient import PersistentGate, SafeSofaScoreClient
from collection.jsonFiles import clear_json_cache
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from storage.pandasStore import atomic_json
from test_providers import Sofa
from transfer import pack, restore


@contextmanager
def windows_default_encoding():
    """Emulate a Windows locale where implicit text reads use cp1252."""
    original_open = Path.open

    def open_file(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if 'b' not in mode and encoding in (None, 'locale'):
            encoding = 'cp1252'
        return original_open(path, mode, buffering, encoding, errors, newline)

    with patch.object(Path, 'open', open_file):
        yield


class UnicodeSofa(Sofa):
    def __init__(self):
        super().__init__()
        self.count = 1

    def iter_season_events(self, *args):
        events = list(super().iter_season_events(*args))
        events[0]['homeTeam']['name'] = 'Borussia Mönchengladbach'
        events[0]['awayTeam']['name'] = 'Łódź'
        return iter(events)

    def lineups(self, match_id):
        payload = super().lineups(match_id)
        payload['home']['players'][0]['player']['name'] = 'João Félix'
        payload['away']['players'][0]['player']['name'] = 'Łukasz Żółć'
        return payload


class PortabilityTests(unittest.TestCase):
    def test_utf8_http_cache_survives_windows_default_encoding(self):
        payload = {'player': {'id': 7, 'name': 'João Félix · Łukasz'}}
        response = Mock(status_code=200)
        response.json.return_value = payload
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory(prefix='Scraper with spaces ') as folder:
            with windows_default_encoding():
                with SafeSofaScoreClient(folder, 'sofascore', session=session) as client:
                    self.assertEqual(client.player(7), payload)
                with SafeSofaScoreClient(folder, 'sofascore', offline=True) as client:
                    self.assertEqual(client.player(7), payload)
            session.get.assert_called_once()

    def test_collection_transfer_and_offline_rebuild_preserve_unicode(self):
        with tempfile.TemporaryDirectory(prefix='Mac Windows transfer ') as folder:
            root = Path(folder)
            data = root / 'first computer' / 'data'
            atomic_json(data / '.soccer-scraper.json', {'format': 1})
            source = collection_directory(data, 'premier-league-2024-25')
            with windows_default_encoding():
                runner = SeasonRunner({'sofascore': UnicodeSofa()}, source,
                                      providers=['sofascore'], report=lambda _: None)
                runner.run('Premier League', '2024/25')
                atomic_json(source / 'job.json', {
                    'competition': 'Premier League', 'season': '2024/25',
                    'providers': ['sofascore'], 'spatial': list(runner.spatial),
                })
                atomic_json(data / 'safety' / 'sofascore.json', {
                    'blocked_until': 2100000000, 'status_code': 403,
                    'reason': 'Vérification requise · Łódź',
                })
                before = pd.read_csv(source / 'player_match_stats.csv')
                archive = pack(data, root / 'collection-state.zip')
                target = restore(archive, root / 'second computer' / 'data')
                gate = PersistentGate(target / 'safety', 'sofascore')
                self.assertEqual(gate.read()['reason'], 'Vérification requise · Łódź')
                self.assertTrue(gate.is_blocked)
                restored = collection_directory(target, 'premier-league-2024-25')
                (restored / 'player_match_stats.csv').unlink()
                clear_json_cache()
                with patch.multiple(build_csvs, DATA_ROOT=target, DATASET_ROOT=root / 'dataset',
                                    COLLECTION_NAMES=(), REFERENCE_CSVS_ONLY=False,
                                    REPLACE_DATASET=True, BUILD_PLAYER_IDS_CSV=True,
                                    BUILD_TEAM_IDS_CSV=True, BUILD_MATCH_IDS_CSV=True,
                                    REFRESH_PUBLISHED_TEAM_IDS=False,
                                    STANDARDIZE_FIXTURE_TEAM_NAMES=False), \
                        patch('api.httpClient.HttpClient._request',
                              side_effect=AssertionError('Unexpected network request')):
                    self.assertEqual(build_csvs.main(), 0)
                after = pd.read_csv(restored / 'player_match_stats.csv')
                pd.testing.assert_frame_equal(before, after)
                self.assertEqual(set(after.player_name), {'João Félix', 'Łukasz Żółć'})
                ids = pd.read_csv(root / 'dataset' / 'player_ids.csv', dtype=str)
                self.assertEqual(set(ids.player_name), {'João Félix', 'Łukasz Żółć'})
                packaged = root / 'dataset' / 'premier-league' / '2024-2025'
                self.assertEqual({p.name for p in packaged.iterdir()},
                                 {'player_match_stats.csv', 'fixtures.csv'})


if __name__ == '__main__':
    unittest.main()

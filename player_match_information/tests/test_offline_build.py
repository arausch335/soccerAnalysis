"""Exercise the retained builders together using collected mock checkpoints."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import build_csvs
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from test_providers import Sofa, Fot


class OfflineBuildTests(unittest.TestCase):
    def test_player_export_packaging_and_ids_use_no_http(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir()
            (data/'.soccer-scraper.json').write_text('{"format":1}')
            source=collection_directory(data,'premier-league-2024-25')
            sofa=Sofa();sofa.count=1
            runner=SeasonRunner({'sofascore':sofa,'fotmob':Fot()},source,
                providers=['sofascore','fotmob'],report=lambda _:None)
            runner.run('Premier League','2024/25')
            before=pd.read_csv(source/'player_match_stats.csv')
            (source/'job.json').write_text(json.dumps({
                'competition':'Premier League','season':'2024/25',
                'providers':['sofascore','fotmob'],'spatial':list(runner.spatial)}))
            (source/'player_match_stats.csv').unlink()
            with patch.multiple(build_csvs,DATA_ROOT=data,DATASET_ROOT=root/'dataset',
                    COLLECTION_NAMES=(),REFERENCE_CSVS_ONLY=False,REPLACE_DATASET=True,
                    BUILD_PLAYER_IDS_CSV=True,BUILD_TEAM_IDS_CSV=True,BUILD_MATCH_IDS_CSV=True,
                    REFRESH_PUBLISHED_TEAM_IDS=False,STANDARDIZE_FIXTURE_TEAM_NAMES=False), \
                    patch('api.safeClient.CachedTransport._get',side_effect=AssertionError('unexpected HTTP')):
                self.assertEqual(build_csvs.main(),0)
            after=pd.read_csv(source/'player_match_stats.csv')
            pd.testing.assert_frame_equal(before,after)
            packaged=root/'dataset/premier-league/2024-2025'
            self.assertEqual({file.name for file in packaged.iterdir()},
                             {'player_match_stats.csv','fixtures.csv'})
            ids=pd.read_csv(root/'dataset/player_ids.csv',dtype=str,keep_default_na=False)
            self.assertEqual(set(zip(ids.sofascore_id,ids.fotmob_id)),{('7','77'),('8','88')})
            self.assertEqual(set(ids.whoscored_id),{''})
            matches=pd.read_csv(root/'dataset/match_ids.csv',dtype=str,keep_default_na=False)
            self.assertEqual(matches.sofascore_id.tolist(),['1'])
            self.assertEqual(matches.fotmob_id.tolist(),['101'])

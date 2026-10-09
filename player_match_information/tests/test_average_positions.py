"""Average locations use provider fields, with no shot/action map processing."""
import json
from pathlib import Path
import tempfile
import unittest

from collection.coordinates import extract_average_position
from collection.seasonRunner import SeasonRunner
import test_providers
import pandas as pd


class AveragePositionTests(unittest.TestCase):
    def setUp(self):
        self.match=test_providers.ProviderTests().baseline()
        self.player=self.match.home_lineup.players[0]

    def save(self,folder,payload,**envelope):
        Path(folder,'average.json').write_text(json.dumps({'payload':payload,**envelope}))
        self.match.metadata['sofascore_spatial_files']={'average_positions':'average.json'}

    def test_copies_reported_home_and_away_positions_without_averaging(self):
        with tempfile.TemporaryDirectory() as folder:
            self.save(folder,{
                'home':[{'player':{'id':7},'averageX':0,'averageY':23.75,'pointsCount':30}],
                'away':[{'player':{'id':8},'averageX':81.5,'averageY':90}]},
                provider='sofascore',key='average_positions/1')
            self.assertEqual(extract_average_position(self.match,self.player,folder),
                             {'average_x':0,'average_y':23.75})
            self.assertEqual(extract_average_position(self.match,self.match.away_lineup.players[0],folder),
                             {'average_x':81.5,'average_y':90})

    def test_missing_player_or_coordinate_stays_blank(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(extract_average_position(self.match,self.player,folder),
                             {'average_x':None,'average_y':None})
            self.save(folder,{'home':[{'player':{'id':8},'averageX':50,'averageY':60}]})
            self.assertEqual(extract_average_position(self.match,self.player,folder),
                             {'average_x':None,'average_y':None})
            self.save(folder,{'home':[{'player':{'id':7},'averageX':50}]})
            self.assertEqual(extract_average_position(self.match,self.player,folder),
                             {'average_x':50,'average_y':None})

    def test_invalid_coordinates_and_duplicate_player_ids_stop_export(self):
        for points in ([{'player':{'id':7},'averageX':True,'averageY':50}],
                       [{'player':{'id':7},'averageX':float('inf'),'averageY':50}],
                       [{'player':{'id':7}}, {'player':{'id':7}}]):
            with self.subTest(points=points),tempfile.TemporaryDirectory() as folder:
                self.save(folder,{'home':points})
                with self.assertRaises(ValueError):
                    extract_average_position(self.match,self.player,folder)

    def test_wrong_match_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            self.save(folder,{'home':[]},provider='sofascore',key='average_positions/2')
            with self.assertRaisesRegex(ValueError,'identity mismatch'):
                extract_average_position(self.match,self.player,folder)

    def test_fast_exports_average_locations_and_makes_no_other_spatial_requests(self):
        class AverageOnlySofa(test_providers.Sofa):
            def __init__(self):
                super().__init__();self.count=1;self.average_calls=[]
            def average_positions(self,mid):
                self.average_calls.append(mid)
                return {'home':[{'player':{'id':7},'averageX':25,'averageY':40}],
                        'away':[{'player':{'id':8},'averageX':75,'averageY':60}]}
            def shotmap(self,*args):raise AssertionError('shot map requested in fast mode')
            def player_heatmap(self,*args):raise AssertionError('heatmap requested in fast mode')
            def player_rating_breakdown(self,*args):raise AssertionError('action map requested in fast mode')

        with tempfile.TemporaryDirectory() as folder:
            sofa=AverageOnlySofa()
            runner=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'],
                spatial=('average_positions',),report=lambda _:None)
            self.assertTrue(runner.run('Premier League','2024/25').status.eq('complete').all())
            players=pd.read_csv(Path(folder,'player_match_stats.csv'))
            self.assertEqual(players.average_x.tolist(),[25,75])
            self.assertEqual(players.average_y.tolist(),[40,60])
            self.assertTrue(players.touch_count.isna().all())
            self.assertTrue(players.heatmap_xy.isna().all())
            self.assertEqual(sofa.average_calls,[1])
            runner.run('Premier League','2024/25')
            self.assertEqual(sofa.average_calls,[1])

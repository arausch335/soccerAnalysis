"""Regression cases for source completeness and missing-versus-zero semantics."""
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from api import FotMobLoader
from api.fotMobClient import without_incident_stream
from api.statistics import fotmob_stats, synthesize
from collection.fotmobExports import shot_features, source_rows
from collection.seasonRunner import SeasonRunner
from collection.spatial import extract_spatial_features, extract_pass_features, extract_dribble_features
from test_providers import fot_payload, Fot


class FotMobExportTests(unittest.TestCase):
    def load(self,payload):
        loader=FotMobLoader();season=loader.select_season(47,'Premier League',2024,'2024/2025')
        return loader.load_match(payload,season)

    def test_missing_starting_player_retains_match_and_unknown_minutes(self):
        payload=fot_payload();del payload['content']['playerStats']['77']
        match=self.load(payload);p=match.home_lineup.players[0]
        self.assertTrue(p.starter)
        self.assertIsNone(p.minutes_played)
        self.assertEqual(p.stats.raw['_providers']['fotmob']['data_status'],'missing_player_stats')
        self.assertIsNone(p.stats.raw['_combined']['passes_attempted'])
        self.assertEqual(match.away_lineup.players[0].stats.raw['_combined']['passes_attempted'],10)

    def test_fotmob_incident_stream_is_ignored_for_participation(self):
        payload=fot_payload();team=payload['content']['lineup']['homeTeam']
        team['subs']=team.pop('starters');team['starters']=[]
        del payload['content']['playerStats']['77']
        payload['content']['matchFacts']={'events':{'events':[{'type':'Substitution','isHome':True,
            'swap':[{'id':'77'},{'id':'999'}]}]}}
        match=self.load(payload);p=match.home_lineup.players[0]
        self.assertFalse(p.did_play)
        self.assertIsNone(p.minutes_played)

    def test_fotmob_incident_stream_is_removed_without_losing_other_match_facts(self):
        payload=fot_payload()
        payload['content']['matchFacts']={'events':{'events':[{'type':'Goal'}]},'infoBox':{'Attendance':123}}
        cleaned=without_incident_stream(payload)
        self.assertNotIn('events',cleaned['content']['matchFacts'])
        self.assertEqual(cleaned['content']['matchFacts']['infoBox'],{'Attendance':123})
        self.assertIn('events',payload['content']['matchFacts'])

    def test_new_mappings_and_partial_duels(self):
        def player(values):return {'stats':[{'stats':{k:{'key':k,'stat':v} for k,v in values.items()}}]}
        raw,metrics=fotmob_stats(player({'duel_won':{'value':8},'duel_lost':{'value':13},
            'rating_title':{'value':7.92},'fantasy_points':{'value':8,'bonus':1},
            'shot_accuracy':{'value':2,'total':3},'physical_metrics_walking':{'value':123,'total':1000},
            'defensive_actions':{'value':5},'keeper_sweeper':{'value':2}}))
        self.assertEqual(metrics['total_duels_attempted'],21)
        self.assertEqual(metrics['total_duels_completed'],8)
        self.assertEqual(metrics['fotmob_fantasy_points'],8)
        self.assertEqual(metrics['fotmob_fantasy_bonus'],1)
        self.assertAlmostEqual(metrics['fotmob_shot_accuracy'],2/3)
        self.assertEqual(metrics['fotmob_walking_distance_m'],123)
        self.assertEqual(metrics['fotmob_defensive_actions'],5)
        _,partial=fotmob_stats(player({'duel_won':{'value':8}}))
        self.assertIsNone(partial['total_duels_attempted'])
        self.assertEqual(partial['total_duels_completed'],8)
        _,empty=fotmob_stats(player({'shot_accuracy':{'value':0,'total':0}}))
        self.assertIsNone(empty['fotmob_shot_accuracy'])
        self.assertTrue(all(v is None for v in synthesize({}, {'fotmob':{'metrics':metrics}},False,True)['metrics'].values()))

    def test_unavailable_maps_are_blank_but_observed_empty_maps_are_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            match=self.load(fot_payload());p=match.home_lineup.players[0]
            heat=extract_spatial_features(match,p,folder)
            passes,_,_=extract_pass_features(match,p,folder)
            dribbles,_=extract_dribble_features(match,p,folder,heat)
            self.assertIsNone(heat['touch_count']);self.assertIsNone(heat['heatmap_source'])
            self.assertIsNone(heat['important_action_count'])
            self.assertIsNone(passes['pass_attempt_count']);self.assertIsNone(passes['pass_map_source'])
            self.assertIsNone(dribbles['dribble_attempts']);self.assertIsNone(dribbles['failed_dribble_danger'])
            Path(folder,'maps.json').write_text(json.dumps({'payload':{'passes':[],'dribbles':[]}}))
            Path(folder,'heat.json').write_text(json.dumps({'payload':{'heatmap':[]}}))
            match.metadata['sofascore_spatial_files']={'77':{'rating_breakdown':'maps.json','heatmap':'heat.json'}}
            heat=extract_spatial_features(match,p,folder)
            passes,_,_=extract_pass_features(match,p,folder)
            dribbles,_=extract_dribble_features(match,p,folder,heat)
            self.assertEqual(heat['touch_count'],0)
            self.assertEqual(passes['pass_attempt_count'],0)
            self.assertEqual(dribbles['dribble_attempts'],0)

    def test_shot_summary_preserves_missing_values_and_excludes_own_goals(self):
        saved={'payload':{'content':{'shotmap':{'shots':[
            {'playerId':77,'teamId':501,'x':90,'y':34,'expectedGoals':.2,'shotType':'Header','situation':'RegularPlay'},
            {'playerId':77,'teamId':501,'x':100,'y':34,'expectedGoals':.9,'isOwnGoal':True}]}}}}
        result=shot_features(saved,77,501,True,1)
        self.assertEqual(result['fotmob_shot_count'],1)
        self.assertEqual(result['fotmob_shot_xg_sum'],.2)
        self.assertIsNone(result['fotmob_shot_xgot_sum'])
        self.assertEqual(result['fotmob_shot_mean_x_raw'],90)
        self.assertTrue(result['fotmob_shotmap_reconciled'])
        self.assertIsNone(shot_features(None,77,501,True,0)['fotmob_shot_count'])
        self.assertEqual(shot_features(saved,88,502,True,0)['fotmob_shot_count'],0)
        self.assertIsNone(shot_features(saved,88,502,False,None)['fotmob_shot_count'])

    def test_team_statistics_include_matchweek(self):
        payload=fot_payload()
        payload['general'].update(homeTeam={'id':501,'name':'Liverpool'},
                                  awayTeam={'id':502,'name':'Other'})
        payload['content']['stats']={'Periods':{'All':{'stats':[
            {'key':'top_stats','stats':[{'key':'BallPossesion','title':'Ball possession',
                'stats':[55,45],'type':'graph','format':'integer'}]}]}}}
        match=self.load(payload);match.round_number=7
        saved={'provider':'fotmob','key':'match/101','fetched_at':'now','payload':payload}
        _,_,teams=source_rows(match,saved)
        self.assertEqual(len(teams),2)
        self.assertEqual({row['matchweek'] for row in teams},{7})
        self.assertEqual({row['competition'] for row in teams},{'Premier League'})
        self.assertEqual({row['team'] for row in teams},{'Liverpool','Other'})

    def test_cached_rebuild_refreshes_mappings_and_recovers_missing_stats_without_http(self):
        with tempfile.TemporaryDirectory() as folder:
            fot=Fot();runner=SeasonRunner({'fotmob':fot},folder,providers=['fotmob'],baseline_provider='fotmob',spatial=(),report=lambda _:None)
            runner.run('Premier League','2024/25')
            path=runner.cache.path('fotmob','match/101');saved=json.loads(path.read_text())
            del saved['payload']['content']['playerStats']['77']
            # An unfamiliar future key remains in the long-form raw export.
            saved['payload']['content']['playerStats']['88']['stats'][0]['stats']['New']={'key':'future_stat','stat':{'value':17}}
            saved['payload']['content']['playerStats']['999']={'id':999,'teamId':501,
                'name':'Different Player','stats':[{'stats':{'Minutes':{'key':'minutes_played','stat':{'value':90}}}}]}
            path.write_text(json.dumps(saved))
            original=path.read_bytes();calls=list(fot.calls)
            result=runner.rebuild_cached()
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(fot.calls,calls)
            self.assertEqual(result['complete'],3)
            quality=pd.read_csv(Path(folder,'data_quality.csv'))
            self.assertEqual(set(quality.issue),{'missing_player_stats','player_stats_not_in_lineup'})
            flat=pd.read_csv(Path(folder,'player_match_stats.csv'))
            row=flat[(flat.match_id==101)&(flat.player_id==77)].iloc[0]
            self.assertTrue(row.appeared);self.assertTrue(pd.isna(row.minutes_played))
            raw=pd.read_csv(Path(folder,'fotmob_raw_player_stats.csv'))
            self.assertIn('future_stat',set(raw.field))
            self.assertIn(999,set(raw.provider_player_id))
            self.assertNotIn(999,set(flat.player_id))

    def test_offline_export_redecodes_cached_provider_stats(self):
        with tempfile.TemporaryDirectory() as folder:
            fot=Fot();runner=SeasonRunner({'fotmob':fot},folder,providers=['fotmob'],
                baseline_provider='fotmob',spatial=(),report=lambda _:None)
            runner.run('Premier League','2024/25')
            path=runner.cache.path('fotmob','match/101');saved=json.loads(path.read_text())
            stats=saved['payload']['content']['playerStats']['77']['stats'][0]['stats']
            stats['Rating']={'key':'rating_title','stat':{'value':8.4}}
            path.write_text(json.dumps(saved))
            calls=list(fot.calls)
            events=json.loads(Path(folder,'schedule.json').read_text())['events']
            runner._export(events)
            self.assertEqual(fot.calls,calls)
            flat=pd.read_csv(Path(folder,'player_match_stats.csv'))
            row=flat[(flat.match_id==101)&(flat.player_id==77)].iloc[0]
            self.assertEqual(row.fotmob_rating,8.4)


if __name__=='__main__':unittest.main()

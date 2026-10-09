"""Regression cases for source completeness and missing-versus-zero semantics."""
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from api import FotMobLoader
from api.fotMobClient import without_incident_stream
from api.statistics import fotmob_stats, synthesize
from collection.seasonRunner import SeasonRunner
from test_providers import fot_payload, Fot, Sofa


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

    def test_main_scraper_keeps_fotmob_enrichment_and_offline_redecode(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();sofa.count=1;fot=Fot()
            runner=SeasonRunner({'sofascore':sofa,'fotmob':fot},folder,
                providers=['sofascore','fotmob'],spatial=(),report=lambda _:None)
            self.assertTrue(runner.run('Premier League','2024/25').status.eq('complete').all())
            identities=pd.read_csv(Path(folder,'identities.csv'))
            self.assertEqual(set(identities.provider),{'fotmob'})
            self.assertEqual(set(identities.provider_player_id),{77,88})
            cache=runner.cache.path('fotmob','match/101')
            saved=json.loads(cache.read_text())
            for player in saved['payload']['content']['playerStats'].values():
                player['stats'][0]['stats']['Defending']={
                    'key':'defensive_actions','stat':{'value':5}}
            cache.write_text(json.dumps(saved))
            calls=(list(sofa.calls),list(fot.calls))
            events=json.loads(Path(folder,'schedule.json').read_text())['events']
            runner._export(events)
            master=pd.read_csv(Path(folder,'player_match_stats.csv'))
            self.assertEqual(master.fotmob_defensive_actions.tolist(),[5,5])
            self.assertEqual(master.passes_completion_rate.tolist(),[.8,.8])
            self.assertEqual((sofa.calls,fot.calls),calls)
            self.assertFalse(Path(folder,'fotmob_player_stats.csv').exists())







if __name__=='__main__':unittest.main()

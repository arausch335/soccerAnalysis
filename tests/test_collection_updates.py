"""Regression checks for standalone collection, portability and match coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import pandas as pd
import config
from tests.legacy_collectors import FotMobOnlyRunner, player_rows
from collection.playerStatsSchema import PLAYER_MATCH_COLUMNS, FOTMOB_PLAYER_MATCH_COLUMNS, fotmob_column
from tests.legacy_collectors import WhoScoredEventRunner
from providers.cache import ResponseCache
from providers.events import fotmob_fixture
from providers.httpClient import ProviderRequestError
from providers.safeClient import SafeWhoScoredClient
from storage.pandasStore import atomic_json
from review_data import build_report
from tests.test_providers import Fot, fot_payload, event, lineup
from tests.test_whoscored_events import WhoScoredOnly


def quiet(*args,**kwargs):pass


class CollectionUpdates(unittest.TestCase):
    def test_fotmob_schema_missing_values_unused_subs_and_ids(self):
        payload=fot_payload();fixture=fotmob_fixture(Fot().season_events()[0])
        payload['content']['lineup']['homeTeam']['subs']=[{'id':99,'name':'Unused','shirtNumber':'5'}]
        rows=player_rows(payload,'Premier League','2024/25',fixture)
        self.assertEqual(tuple(rows[0]),FOTMOB_PLAYER_MATCH_COLUMNS)
        self.assertEqual(tuple(fotmob_column(c) for c in PLAYER_MATCH_COLUMNS),tuple(rows[0]))
        player=rows[0]
        self.assertEqual(player['fmb_match_id'],'fmb_101')
        self.assertEqual(player['fmb_player_id'],'fmb_77')
        self.assertEqual(player['fmb_team_id'],'fmb_501')
        self.assertEqual(player['fmb_passes_attempted'],10)
        self.assertEqual(player['fmb_passes_completion_rate'],.8)
        self.assertEqual(player['fmb_dribbles_completed'],0)
        self.assertIsNone(player['ball_carries'])
        self.assertIsNone(player['average_x'])
        self.assertIsNone(player['sofascore_rating'])
        unused=next(r for r in rows if r['fmb_player_id']=='fmb_99')
        self.assertFalse(unused['fmb_appeared'])
        self.assertIsNone(unused['fmb_goals'])
        self.assertIsNone(unused['fmb_minutes_played'])

    def test_fotmob_resume_limit_preserves_successes_and_logs_match_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Fot();logs=[]
            runner=FotMobOnlyRunner(client,tmp,logs.append)
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertEqual(client.calls,[101,102,103])
            client.calls.clear()
            self.assertEqual(runner.run('EPL','2024/25',limit=1),0)
            self.assertEqual(client.calls,[])
            frame=pd.read_csv(Path(tmp)/'player_match_stats.csv')
            self.assertEqual(len(frame),6)
            self.assertTrue(any('Premier League 2024-2025 | match 101 | Liverpool vs Other' in x for x in logs))
            self.assertNotIn('matchFacts',json.dumps(read_payload(tmp)))
            with self.assertRaisesRegex(ValueError,'another competition'):
                runner.run('La Liga','2024/25')

    def test_fotmob_refusal_stops_after_one_match_and_keeps_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Fot();client.blocked=True
            runner=FotMobOnlyRunner(client,tmp,quiet)
            self.assertEqual(runner.run('EPL','2024/25'),2)
            self.assertEqual(client.calls,[101])
            frame=pd.read_csv(Path(tmp)/'player_match_stats.csv')
            self.assertTrue(frame.empty)
            self.assertEqual(tuple(frame.columns),FOTMOB_PLAYER_MATCH_COLUMNS)
            client.blocked=False
            self.assertEqual(runner.run('EPL','2024/25'),0)

    def test_wrong_match_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Fot();client.match_details=lambda mid:fot_payload(999)
            runner=FotMobOnlyRunner(client,tmp,quiet)
            self.assertEqual(runner.run('EPL','2024/25',limit=1),2)
            self.assertEqual(list((Path(tmp)/'raw').glob('*/*.json')),[])

    def test_verified_stage_ids_skip_html_discovery_in_both_workflows(self):
        from collection.seasonRunner import SeasonRunner
        from collection.selection import competition_by_name
        from tests.test_providers import Sofa
        with tempfile.TemporaryDirectory() as tmp, patch.object(config,'WHOSCORED_STAGE_IDS',{'Premier League 2024-2025':[11,12]}):
            client=WhoScoredOnly();client.seasons=Mock(side_effect=AssertionError('HTML directory requested'))
            client.season_stages=Mock(side_effect=AssertionError('HTML season requested'))
            logs=[]
            runner=WhoScoredEventRunner(client,Path(tmp)/'who',lambda s,**kw:logs.append(s))
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertTrue(any('Premier League 2024-2025 | match 11 | Home vs Away' in x for x in logs))
            season=SeasonRunner({'sofascore':Sofa(),'whoscored':client},Path(tmp)/'integrated',
                                providers=('sofascore','whoscored'),spatial=(),report=quiet)
            fixtures,errors=season._fixtures(competition_by_name('EPL'),'2024/2025',[event()])
            self.assertFalse(errors)
            self.assertEqual(len(fixtures['whoscored']),2)

    def test_windows_cp1252_default_does_not_affect_utf8_cache_resume(self):
        original=Path.open
        def windows_open(path,mode='r',buffering=-1,encoding=None,errors=None,newline=None):
            if 'b' not in mode and encoding is None:encoding='cp1252'
            return original(path,mode,buffering,encoding,errors,newline)
        with tempfile.TemporaryDirectory() as tmp:
            cache=ResponseCache(Path(tmp)/'raw')
            # UTF-8 for U+0141 includes byte 0x81, which is undefined in cp1252.
            expected={'name':'Łukasz — João'}
            cache.fetch('whoscored','match/1',lambda:expected)
            with SafeWhoScoredClient(tmp,session=Mock()) as client:
                client._cached('test',None,'text',lambda:'Łukasz')
            with patch.object(Path,'open',windows_open):
                self.assertEqual(cache.fetch('whoscored','match/1',lambda:None),expected)
                with SafeWhoScoredClient(tmp,offline=True,session=Mock()) as client:
                    self.assertEqual(client._cached('test',None,'text',lambda:None),'Łukasz')

    def test_review_uses_full_schedule_and_each_provider_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);normal=root/'premier-league'/'2024-2025'
            atomic_json(normal/'schedule.json',{'events':[event(i) for i in (1,2,3,4)]})
            atomic_json(normal/'config.json',{'baseline_provider':'sofascore'})
            cache=ResponseCache(normal/'raw')
            for mid in (1,2,3):cache.fetch('sofascore',f'lineups/{mid}',lineup)
            who=root/'premier-league'/'2024-2025'
            runner=WhoScoredEventRunner(WhoScoredOnly(),who,quiet)
            runner.run('EPL','2024/25',limit=1)
            frame=build_report(root,['EPL'],['2024/25'])
            sofa=frame[frame.provider=='sofascore'].iloc[0]
            ws=frame[frame.provider=='whoscored'].iloc[0]
            self.assertEqual((sofa.complete,sofa.total,sofa.percent),(3,4,75))
            self.assertEqual((ws.complete,ws.total,ws.percent),(1,2,50))
            path=ResponseCache(who/'raw').path('whoscored','match/11')
            saved=json.loads(path.read_text());saved['payload']['match_id']=999;atomic_json(path,saved)
            ws=build_report(root,['EPL'],['2024/25']).iloc[0]
            self.assertEqual(ws.complete,0)
            self.assertEqual(ws.percent,0)
            unknown=build_report(root,['La Liga'],['2024/25'])
            self.assertTrue(unknown.percent.isna().all())

    def test_review_detects_empty_player_stats_and_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            normal=Path(tmp)/'premier-league'/'2024-2025'
            atomic_json(normal/'schedule.json',{'events':[event(1),event(2)]})
            payload=lineup();payload['away']['players'][0]['statistics']={}
            cache=ResponseCache(normal/'raw');cache.fetch('sofascore','lineups/1',lambda:payload)
            path=cache.path('sofascore','lineups/2');path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{bad')
            sofa=build_report(tmp,['EPL'],['2024/25']).iloc[1]
            self.assertEqual((sofa.complete,sofa.total,sofa.percent),(0,2,0))
            self.assertIn('1 invalid',sofa.note)


def read_payload(tmp):
    return json.loads(next((Path(tmp)/'raw'/'fotmob').glob('*.json')).read_text())


if __name__=='__main__':unittest.main()

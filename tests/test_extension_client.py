from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import config
from collection.playerStatsSchema import PLAYER_MATCH_COLUMNS
from collection.seasonRunner import SeasonRunner
from providers.transports.bridge import ExtensionBridge, acquire_bridge, extension_scope
from providers.transports.sofascore_extension import ExtensionSofaScoreClient
from providers.httpClient import ProviderRequestError
from providers.transports.sofascore import create_sofascore_client
from tests.test_extension_payloads import extension_settings
from tests.test_providers import event, lineup, Sofa


def quiet(*args,**kwargs):pass


class FakeBridge:
    def __init__(self):
        self.calls=[];self.reads=[];self.denied=0;self.wrong=False
        self.source='https://www.sofascore.com/event/1'

    def request(self,action,args,**kwargs):
        self.calls.append((action,deepcopy(args)))
        if action=='navigate':self.source=args['url']
        result={'ok':True,'source_url':self.source}
        if action!='page' or args['operation']!='read':return result
        key=args['endpoint'];self.reads.append(key)
        if self.denied:
            self.denied-=1
            return dict(result,status=403)
        if key.endswith('/seasons'):payload={'seasons':[{'id':61627,'year':'24/25','name':'Premier League 24/25'}]}
        elif '/events/' in key:payload={'events':[event(1)],'hasNextPage':False}
        elif key=='event/1':payload={'event':event(999 if self.wrong else 1)}
        elif key.endswith('/lineups'):payload=lineup()
        elif '/player/' in key and key.endswith('/statistics'):
            pid=int(key.split('/')[3]);payload={'player':{'id':pid},'team':{'id':44 if pid==7 else 2},
                'statistics':{'minutesPlayed':90,'totalPass':10,'accuratePass':8}}
        elif key.endswith('/average-positions'):payload={'home':[],'away':[]}
        elif key.endswith('/statistics'):payload={'statistics':[]}
        elif key.endswith('/incidents'):payload={'incidents':[]}
        elif key.endswith('/graph/momentum'):payload={'graphPoints':[]}
        else:return dict(result,status=404)
        return dict(result,status=200,payload=payload)


class ExtensionClientTests(unittest.TestCase):
    def test_roundtrip_cache_and_offline_replay_never_launch_playwright(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge=FakeBridge()
            with patch('subprocess.Popen',side_effect=AssertionError('No browser launch')):
                client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
                self.assertEqual(client.event(1),{'event':event(1)})
                self.assertEqual(client.event(1),{'event':event(1)})
                self.assertEqual(bridge.reads,['event/1'])
                saved=client.cache.read('event/1',None,'json')
                self.assertEqual(saved['transport'],'extension')
                replay=ExtensionSofaScoreClient(folder,extension_settings(),bridge=FakeBridge(),offline=True,report=quiet)
                self.assertEqual(replay.event(1),{'event':event(1)})
                self.assertEqual(replay.bridge.calls,[])

    def test_wrong_match_and_unapproved_endpoint_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge=FakeBridge();bridge.wrong=True
            client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            with self.assertRaisesRegex(ValueError,'different match'):client.event(1)
            self.assertFalse(client.cache.path('event/1',None,'json').exists())
            for endpoint in ('https://evil.test','event/1/../../cookies','player/7','event/1/login'):
                with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):client.get(endpoint)
            with self.assertRaises(ValueError):client.get('event/1',{'redirect':'evil'})

    def test_manual_403_resume_is_bounded_and_429_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge=FakeBridge();bridge.denied=1
            client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            self.assertEqual(client.event(1),{'event':event(1)})
            self.assertEqual(sum(action=='verify' for action,_ in bridge.calls),1)
            verify=next(args for action,args in bridge.calls if action=='verify')
            self.assertFalse(verify['visible_challenge'])
            self.assertEqual(verify['match_url'],'https://www.sofascore.com/event/1')
            bridge.denied=2;client.refresh_cache=True
            with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,403)
            self.assertFalse(client.request_gate.is_blocked)
            with patch.object(bridge,'request',return_value={'ok':True,'status':429,'retryAfter':'172800'}):
                with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,429)
            self.assertTrue(client.request_gate.is_blocked)

    def test_one_match_exports_the_normal_schema_and_matches_normal_player_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bridge=FakeBridge()
            extension=ExtensionSofaScoreClient(root,extension_settings(),bridge=bridge,report=quiet)
            runner=SeasonRunner({'sofascore':extension},root/'extension-season',providers=['sofascore'],
                spatial=('average_positions',),report=quiet)
            result=runner.run('EPL','2024/25')
            self.assertEqual(result.status.tolist(),['complete'])
            sofa=Sofa();sofa.count=1
            baseline=SeasonRunner({'sofascore':sofa},root/'normal-season',providers=['sofascore'],
                spatial=('average_positions',),report=quiet)
            baseline.run('EPL','2024/25')
            actual=pd.read_csv(root/'extension-season'/'player_match_stats.csv')
            expected=pd.read_csv(root/'normal-season'/'player_match_stats.csv')
            self.assertEqual(tuple(actual.columns),PLAYER_MATCH_COLUMNS)
            pd.testing.assert_frame_equal(actual,expected)
            coverage=json.loads((root/'extension-season'/'browser_coverage.json').read_text())
            self.assertEqual(coverage['1']['transport'],'extension')
            self.assertTrue(coverage['1']['complete'])
            self.assertIn('event/1/player/7/statistics',bridge.reads)
            self.assertIn('event/1/player/8/statistics',bridge.reads)
            self.assertTrue(actual.ball_carries.isna().all())
            report=root/'extension-season'/'collection_coverage.csv'
            report.unlink();before=len(bridge.calls)
            self.assertEqual(runner.run('EPL','2024/25').status.tolist(),['complete'])
            self.assertEqual(len(bridge.calls),before)
            self.assertTrue(report.exists())
            self.assertEqual(set(pd.read_csv(report).status),{'complete'})

    def test_disconnect_stops_remaining_games_without_repeated_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bridge=FakeBridge();original=bridge.request
            def request(action,args,**kwargs):
                if action=='page' and args.get('endpoint')=='event/1/lineups':
                    bridge.unavailable=True
                    raise ProviderRequestError('Extension disconnected')
                result=original(action,args,**kwargs)
                if action=='page' and '/events/' in args.get('endpoint',''):
                    result['payload']['events']=[event(1),event(2)]
                return result
            bridge.request=request
            client=ExtensionSofaScoreClient(root,extension_settings(),bridge=bridge,report=quiet)
            result=SeasonRunner({'sofascore':client},root/'season',providers=['sofascore'],
                spatial=(),report=quiet).run('EPL','2024/25',build_csvs=False)
            self.assertEqual(result.status.tolist(),['failed','pending'])

    def test_detail_connection_failure_produces_one_error_and_keeps_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bridge=FakeBridge();original=bridge.request;failed=[];messages=[]
            def request(action,args,**kwargs):
                if action=='page' and args.get('endpoint')=='event/1/player/7/statistics':
                    failed.append(args['endpoint']);bridge.unavailable=True
                    raise ProviderRequestError('Recovery exhausted')
                result=original(action,args,**kwargs)
                if action=='page' and '/events/' in args.get('endpoint',''):
                    result['payload']['events']=[event(1),event(2)]
                return result
            bridge.request=request
            client=ExtensionSofaScoreClient(root,extension_settings(),bridge=bridge,report=quiet)
            result=SeasonRunner({'sofascore':client},root/'season',providers=['sofascore'],
                spatial=('average_positions',),report=messages.append).run('EPL','2024/25',build_csvs=False)
            self.assertEqual(result.status.tolist(),['partial','pending'])
            self.assertEqual(failed,['event/1/player/7/statistics'])
            self.assertEqual(result.iloc[0]['error'],'player/7/statistics: Recovery exhausted')
            self.assertTrue((root/'season'/'match_checkpoints'/'1').is_dir())
            self.assertNotIn('event/1/player/8/statistics',bridge.reads)
            self.assertTrue(any('diagnostic log' in message for message in messages))
            coverage=pd.read_csv(root/'season'/'collection_coverage.csv').set_index('resource')
            self.assertEqual(coverage.loc['player/7/statistics','status'],'failed')
            self.assertEqual(coverage.loc['player/8/statistics','status'],'not_attempted')
            self.assertEqual(coverage.loc['average_positions','status'],'not_attempted')
            self.assertEqual(coverage.loc['player/7/statistics','competition'],'Premier League')
            self.assertTrue(result.attrs['sofascore_blocked'])
            self.assertNotIn('event/2/lineups',bridge.reads)


    def test_partial_details_preserve_prior_success_and_record_missing_and_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge=FakeBridge();original=bridge.request
            def request(action,args,**kwargs):
                if action=='page' and args.get('endpoint')=='event/1/player/8/statistics':
                    bridge.unavailable=True
                    raise ProviderRequestError('Connection interrupted after recovery')
                result=original(action,args,**kwargs)
                if action=='page' and args.get('endpoint')=='event/1/player/7/statistics':
                    result['payload']['statistics']['totalPass']=77
                return result
            bridge.request=request
            client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            source=lineup()
            source['home']['players'].append({'player':{'id':99,'name':'Unused substitute'},
                'substitute':True,'statistics':{}})
            detailed,coverage=client.collect_details(event(1),source)
            self.assertEqual(detailed['home']['players'][0]['statistics']['totalPass'],77)
            self.assertEqual(coverage['coverage']['player/7/statistics'],'complete')
            self.assertEqual(coverage['coverage']['player/8/statistics'],'failed')
            self.assertEqual(coverage['coverage']['statistics'],'not_attempted')
            self.assertEqual(coverage['skipped']['player/99/statistics'],'not_played')
            self.assertEqual(len(coverage['errors']),1)
            saved=client.cache.read('event/1/lineups',None,'json')
            self.assertEqual(saved['payload']['home']['players'][0]['statistics']['totalPass'],77)



if __name__=='__main__':unittest.main()

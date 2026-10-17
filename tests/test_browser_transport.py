"""Transport compatibility, identity, fallback, rate limits and full-data coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import config
from collection.seasonRunner import SeasonRunner
from providers.browserSofaScore import BrowserSofaScoreClient, BrowserGate, validate_payload, event_url
from providers.sofaScoreTransport import SofaScoreTransport
from providers.httpClient import ProviderRequestError
from providers.safeClient import PersistentGate
from providers.networkSettings import make_session,save_cookies,redact
from tests.test_providers import Sofa,event,lineup
from storage.pandasStore import atomic_json


def quiet(*args,**kw):pass


def browser_settings():return {**config.TRANSPORT['browser'],'delay_min_seconds':0,'delay_max_seconds':0}


class BrowserTransportTests(unittest.TestCase):
    def test_auto_switches_once_and_never_retries_http_after_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            http=Mock();http.get.side_effect=ProviderRequestError('blocked',status_code=403)
            browser=Mock();browser.get.return_value={'event':event(1)}
            with SofaScoreTransport(tmp,http=http,browser=browser,report=quiet,mode='auto') as client:
                self.assertEqual(client.event(1)['event']['id'],1)
                client.lineups(1)
                self.assertTrue(client.uses_browser)
                http.get.assert_called_once()
                self.assertEqual(browser.get.call_count,2)
                self.assertIs(client.request_gate,browser.request_gate)
            http.close.assert_called_once();browser.close.assert_called_once()

    def test_429_404_401_and_http_only_failures_do_not_launch_browser(self):
        for mode,status in [('auto',429),('auto',404),('auto',401),('http',403)]:
            with self.subTest(mode=mode,status=status),tempfile.TemporaryDirectory() as tmp:
                http=Mock();http.get.side_effect=ProviderRequestError('failure',status_code=status)
                browser=Mock()
                client=SofaScoreTransport(tmp,http=http,browser=browser,mode=mode,report=quiet)
                with self.assertRaises(ProviderRequestError):client.event(1)
                browser.get.assert_not_called()

    def test_browser_mode_makes_no_http_client_or_request(self):
        with tempfile.TemporaryDirectory() as tmp,patch('providers.sofaScoreTransport.SafeSofaScoreClient') as factory:
            browser=Mock();browser.get.return_value={'event':event(1)}
            with SofaScoreTransport(tmp,mode='browser',browser=browser) as client:client.event(1)
            factory.assert_not_called();browser.get.assert_called_once_with('event/1',None)

    def test_invalid_http_match_identity_triggers_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            http=Mock();http.get.return_value={'event':event(2)}
            browser=Mock();browser.get.return_value={'event':event(1)}
            client=SofaScoreTransport(tmp,mode='auto',http=http,browser=browser,report=quiet)
            self.assertEqual(client.event(1)['event']['id'],1)
            self.assertTrue(client.uses_browser)

    def test_saved_browser_data_is_validated_and_offline_never_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=BrowserSofaScoreClient(tmp,browser_settings(),offline=True)
            client._save('event/1',{'event':event(1)},method='website_response')
            with patch.object(client,'_start') as start:
                self.assertEqual(client.event(1)['event']['id'],1)
                with self.assertRaisesRegex(ProviderRequestError,'Offline browser cache miss'):client.event(2)
                start.assert_not_called()
            path=client._cache_path('event/1');saved=json.loads(path.read_text())
            saved['payload']['event']['id']=999;atomic_json(path,saved)
            with self.assertRaisesRegex(ValueError,'different match'):client.event(1)

    def test_native_response_rejects_wrong_match_and_does_not_store_country_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=BrowserSofaScoreClient(tmp,browser_settings())
            response=Mock(url='https://www.sofascore.com/api/v1/event/1',status=200)
            response.request.resource_type='fetch';response.json.return_value={'event':event(2)}
            client._capture(response)
            self.assertFalse(client._cache_path('event/1').exists())
            response.url='https://www.sofascore.com/api/v1/country/alpha2'
            client._capture(response)
            self.assertEqual(client.responses,{})

    def test_shared_429_blocks_browser_but_http_403_does_not(self):
        with tempfile.TemporaryDirectory() as tmp,patch('providers.safeClient.time.time',return_value=1000),patch('providers.browserSofaScore.time.time',return_value=1000):
            http=PersistentGate(Path(tmp)/'safety','sofascore')
            error=ProviderRequestError('HTTP 403',status_code=403);http.block(error)
            browser=BrowserGate(tmp,browser_settings())
            self.assertFalse(browser.is_blocked)
            browser.wait('event/1')
            error=ProviderRequestError('HTTP 429',status_code=429);error.retry_after=172800
            http.block(error)
            self.assertTrue(browser.is_blocked)
            with self.assertRaises(ProviderRequestError):browser.wait('event/2')

    def test_browser_zero_delay_is_valid_and_http_cooldown_preserves_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=PersistentGate(tmp,'sofascore',min_delay=0,max_delay=0)
            gate.wait('one');gate.block(ProviderRequestError('HTTP 403',status_code=403))
            with self.assertRaises(ProviderRequestError) as caught:gate.wait('two')
            self.assertEqual(caught.exception.status_code,403)
            self.assertEqual(caught.exception.attempts,0)

    def test_full_browser_detail_collection_merges_all_played_players(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=BrowserSofaScoreClient(tmp,browser_settings())
            payload=lineup();bench={'player':{'id':9,'name':'Unused'},'substitute':True,'statistics':{}}
            payload['home']['players'].append(bench)
            client.player_event_statistics=Mock(side_effect=lambda mid,pid:{'player':{'id':pid},'statistics':{'minutesPlayed':90,'totalPass':42,'futureStat':123}})
            client.statistics=Mock(return_value={'statistics':[]})
            client.incidents=Mock(return_value={'incidents':[]})
            client.graph=Mock(return_value={'graphPoints':[]})
            result,report=client.collect_details(event(1),payload)
            self.assertEqual(client.player_event_statistics.call_count,2)
            self.assertEqual(result['home']['players'][0]['statistics']['totalPass'],42)
            self.assertEqual(result['away']['players'][0]['statistics']['futureStat'],123)
            self.assertEqual(result['home']['players'][1],bench)
            self.assertEqual(report['errors'],[])
            self.assertEqual(len(report['coverage']),5)
            self.assertEqual(payload['home']['players'][0]['statistics']['totalPass'],10)

    def test_player_detail_wrong_team_is_failed_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=BrowserSofaScoreClient(tmp,browser_settings())
            client.player_event_statistics=Mock(return_value={'team':{'id':999},'statistics':{'totalPass':999}})
            client.statistics=Mock();client.incidents=Mock();client.graph=Mock()
            result,report=client.collect_details(event(1),lineup())
            self.assertEqual(len(report['errors']),2)
            self.assertEqual(result['home']['players'][0]['statistics']['totalPass'],10)

    def test_invalid_spatial_response_and_duplicate_lineup_are_rejected(self):
        with self.assertRaises(ValueError):validate_payload('event/1/player/7/heatmap',{})
        payload=lineup();payload['away']['players'][0]['player']['id']=7
        with self.assertRaisesRegex(ValueError,'Duplicate'):validate_payload('event/1/lineups',payload)

    def test_http_profiles_persist_and_proxy_secret_is_never_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'TEST_SOCCER_PROXY':'http://alice:secret@localhost:8080'}):
            settings={**config.TRANSPORT['http'],'profiles':['chrome131'],'proxy_env':'TEST_SOCCER_PROXY'}
            one,first=make_session(tmp,'sofascore','https://www.sofascore.com/',settings)
            one.cookies.set('test','cookie',domain='www.sofascore.com');save_cookies(one);one.close()
            two,second=make_session(tmp,'sofascore','https://www.sofascore.com/',settings)
            self.assertEqual(first,second)
            self.assertEqual(two.cookies.get('test'),'cookie')
            text=(Path(tmp)/'http_sessions/sofascore/identity.json').read_text()
            self.assertNotIn('secret',text);self.assertNotIn('alice',text)
            self.assertEqual(redact('http://alice:secret@localhost:8080'),'http://[redacted]@localhost:8080')
            two.close()

    def test_browser_details_update_normal_metrics_without_losing_enrichment(self):
        loader=Sofa();
        with tempfile.TemporaryDirectory() as tmp:
            runner=SeasonRunner({'sofascore':loader},tmp,providers=['sofascore'],spatial=(),report=quiet)
            runner.run('EPL','2024/25',limit=1,build_csvs=False)
            from storage import PandasStore
            match=PandasStore(Path(tmp)/'match_checkpoints'/'1').from_csv().matches[1]
            updated=lineup();updated['home']['players'][0]['statistics'].update(totalPass=50,accuratePass=40)
            runner._apply_browser_details(match,updated)
            self.assertEqual(match.home_lineup.players[0].stats.raw['_combined']['passes_attempted'],50)

    def test_fallback_during_spatial_also_collects_full_stats_for_same_match(self):
        class SwitchingSofa(Sofa):
            uses_browser=False
            def average_positions(self,mid):
                self.uses_browser=True
                return {'home':[],'away':[]}
            def collect_details(self,event,payload):
                updated=deepcopy(payload)
                for side in ('home','away'):
                    updated[side]['players'][0]['statistics'].update(totalPass=50,accuratePass=40)
                return updated,{'coverage':{'statistics':'complete'},'errors':[]}
        with tempfile.TemporaryDirectory() as tmp:
            client=SwitchingSofa();client.count=1
            runner=SeasonRunner({'sofascore':client},tmp,providers=['sofascore'],
                                spatial=('average_positions',),report=quiet)
            result=runner.run('EPL','2024/25',build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])
            from storage import PandasStore
            match=PandasStore(Path(tmp)/'match_checkpoints'/'1').from_csv().matches[1]
            self.assertTrue(match.metadata['browser_details']['complete'])
            self.assertEqual(match.home_lineup.players[0].stats.raw['_combined']['passes_attempted'],50)

    def test_captured_rate_limit_does_not_issue_another_browser_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            page=Mock(url='https://www.sofascore.com/')
            client=BrowserSofaScoreClient(tmp,browser_settings(),page=page)
            client.responses[('event/1','')]={'status':429,'headers':{'Retry-After':'172800'}}
            with patch.object(client,'_drive'),patch.object(client,'_diagnostic'):
                with self.assertRaises(ProviderRequestError):client.event(1)
            page.evaluate.assert_not_called()
            self.assertTrue(client.request_gate.is_blocked)

    def test_sofascore_team_statistics_are_exported_and_packaged_offline(self):
        from collection.sofascoreTeams import team_stat_rows
        from storage import PandasStore
        from providers.cache import ResponseCache
        from dataset_builder import package_collection
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'season'
            runner=SeasonRunner({'sofascore':Sofa()},directory,providers=['sofascore'],spatial=(),report=quiet)
            runner.run('EPL','2024/25',limit=1,build_csvs=False)
            store=PandasStore(directory/'match_checkpoints'/'1');dataset=store.from_csv();match=dataset.matches[1]
            cache=ResponseCache(directory/'raw')
            payload={'statistics':[{'period':'ALL','groups':[{'groupName':'Overview','statisticsItems':[
                {'name':'Ball possession','key':'ballPossession','homeValue':60,'awayValue':40}]}]}]}
            cache.fetch('sofascore','statistics/1',lambda:payload)
            match.metadata['raw_response_files']={'sofascore_statistics':str(cache.path('sofascore','statistics/1').relative_to(directory))}
            store.to_csv(dataset)
            rows=team_stat_rows(match,directory)
            self.assertEqual([row['value'] for row in rows],[60,40])
            self.assertEqual({row['provider'] for row in rows},{'sofascore'})
            runner._export([event(1)],compile_events=False)
            combined=pd.read_csv(directory/'team_match_statistics.csv')
            self.assertEqual(combined.value.tolist(),[60,40])
            target=package_collection(directory,Path(tmp)/'packaged',tables=('team_match_stats',))
            packaged=pd.read_csv(target/'team_match_stats.csv')
            self.assertEqual(packaged.provider.tolist(),['sofascore','sofascore'])

    def test_native_match_url_preserves_custom_and_event_ids(self):
        value=event(1);value['customId']='NsU'
        self.assertTrue(event_url(value).endswith('/NsU#id:1'))


if __name__=='__main__':unittest.main()

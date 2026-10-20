"""API/extension switching and storage migration regressions without live providers."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock,patch
from filelock import FileLock,Timeout
from providers.httpClient import ProviderError,ProviderRequestError
from providers.transports.sofascore import SofaScoreTransport
from providers.transports.whoscored import WhoScoredTransport,WhoScoredExtensionGate
from providers.transports.fotmob import FotMobTransport
from providers.identity import match_fixture,team_key
from providers.cache import ResponseCache
from storage.requestCache import RequestCache,saved_response_batch
from storage.layout import system_path,organize_data_root
from storage.pandasStore import atomic_json
from collection.worker import prepare_extension
from types import SimpleNamespace

class WorkflowTests(unittest.TestCase):
    def test_sofascore_api_then_extension_once(self):
        with TemporaryDirectory() as tmp:
            api=Mock();ext=Mock();ext.responses={};ext.observations={};ext.cache_dir=Path(tmp)
            api.get.side_effect=ProviderRequestError('denied',status_code=403)
            ext.get.side_effect=[{'event':{'id':1}},{'event':{'id':2}}];ext.last_transport='extension'
            client=SofaScoreTransport(tmp,http=api,extension=ext,report=lambda *a,**k:None)
            self.assertEqual(client.event(1)['event']['id'],1)
            self.assertEqual(client.event(2)['event']['id'],2)
            self.assertEqual(api.get.call_count,1);self.assertEqual(ext.get.call_count,2)
    def test_whoscored_uses_extension_not_playwright(self):
        with TemporaryDirectory() as tmp:
            api=Mock();ext=Mock();api.match_details.side_effect=ProviderError('challenge')
            ext.match_details.return_value={'match_id':7}
            client=WhoScoredTransport(tmp,http=api,extension=ext,report=lambda *a,**k:None)
            self.assertEqual(client.match_details(7),{'match_id':7});client.match_details(8)
            self.assertEqual(api.match_details.call_count,1);self.assertEqual(ext.match_details.call_count,2)
    def test_fotmob_parser_failure_falls_back(self):
        with TemporaryDirectory() as tmp:
            api=Mock();ext=Mock();api.match_details.side_effect=ProviderError('wrong match')
            ext.match_details.return_value={'general':{'matchId':7}}
            client=FotMobTransport(tmp,http=api,extension=ext,report=lambda *a,**k:None)
            self.assertEqual(client.match_details(7)['general']['matchId'],7)
            self.assertEqual(client.active,'extension');self.assertTrue(ext.refresh_cache)
    def test_rate_limit_is_not_bypassed(self):
        for factory in (SofaScoreTransport,WhoScoredTransport,FotMobTransport):
            with TemporaryDirectory() as tmp:
                api=Mock();ext=Mock();ext.responses={};ext.observations={};ext.cache_dir=Path(tmp)
                api.get.side_effect=ProviderRequestError('slow down',status_code=429)
                api.match_details.side_effect=ProviderRequestError('slow down',status_code=429)
                client=factory(tmp,http=api,extension=ext)
                with self.assertRaises(ProviderRequestError):
                    client.event(7) if factory==SofaScoreTransport else client.match_details(7)
                ext.get.assert_not_called();ext.match_details.assert_not_called()
    def test_extension_is_ready_before_api_work_without_waiting(self):
        with patch('providers.transports.bridge.acquire_bridge') as acquire:
            bridge=Mock();acquire.return_value=(bridge,False)
            prepare_extension(Path('/tmp/test-combined-worker'),[SimpleNamespace(collection_types=('combined',))],False)
            bridge.start.assert_called_once();bridge.request.assert_not_called();bridge.connected.wait.assert_not_called()
    def test_norwich_concordance_keeps_home_away_and_time_validation(self):
        self.assertEqual(team_key('Norwich City'),team_key('Norwich'))
        event={'id':1,'startTimestamp':1000,'homeTeam':{'name':'Norwich City'},'awayTeam':{'name':'Brentford'}}
        fixture={**event,'id':2,'homeTeam':{'name':'Norwich'}}
        self.assertEqual(match_fixture(event,[fixture])['id'],2)
        with self.assertRaises(ValueError):match_fixture(event,[{**fixture,'startTimestamp':1000+86400}])
    def test_provider_error_does_not_poison_shared_bridge(self):
        from providers.transports.bridge import ExtensionBridge
        import threading
        bridge=ExtensionBridge();bridge.start=Mock();bridge.connected.set();bridge.client_id='test-client'
        errors=[]
        def request():
            try:bridge.request('whoscored',{},timeout=1)
            except ProviderRequestError as exc:errors.append(str(exc))
        thread=threading.Thread(target=request);thread.start()
        with bridge.condition:
            bridge.condition.wait_for(lambda:bridge.pending is not None,timeout=1)
            bridge.pending['result']={'ok':False,'error':'WhoScored page incomplete'}
            bridge.condition.notify_all()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive());self.assertTrue(errors);self.assertFalse(bridge.unavailable)

    def test_partial_provider_errors_are_visible_in_important_logs(self):
        from collection.reporting import console_level
        self.assertEqual(console_level('[4/380] Norwich vs Brentford: partial (whoscored: fixture missing)'),'WARNING')
        self.assertEqual(console_level('0 failed / partial'),'INFO')

    def test_whoscored_extension_has_short_pacing(self):
        with TemporaryDirectory() as tmp:
            gate=WhoScoredExtensionGate(tmp)
            self.assertEqual((gate.min_delay,gate.max_delay),(.5,2))

class StorageTests(unittest.TestCase):
    def test_raw_commit_releases_scratch_copy_and_failed_commit_keeps_it(self):
        with TemporaryDirectory() as tmp:
            cache=RequestCache(tmp,'fotmob');raw=ResponseCache(Path(tmp)/'premier-league/2021-2022/raw')
            def request():
                cache.write('matchDetails',{'matchId':7},'json',{'status':200,'payload':{'id':7}})
                return {'id':7}
            raw.fetch('fotmob','match/7',request)
            self.assertFalse(cache.path('matchDetails',{'matchId':7},'json').exists())
            with patch('providers.cache.atomic_json',side_effect=OSError('full disk')):
                with self.assertRaises(OSError):raw.fetch('fotmob','match/8',request)
            self.assertTrue(cache.path('matchDetails',{'matchId':7},'json').exists())
    def test_compression_roundtrip_and_bounded_eviction(self):
        with TemporaryDirectory() as tmp:
            cache=RequestCache(tmp,'whoscored');payload='large repeated response '*10000
            path=cache.write('matches/1/live',None,'text',{'status':200,'payload':payload})
            self.assertLess(path.stat().st_size,5000);self.assertEqual(cache.read('matches/1/live',None,'text')['payload'],payload)
            RequestCache.trim(cache.directory.parent,0);self.assertFalse(path.exists())
    def test_layout_preserves_settings_identity_and_leagues(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);atomic_json(root/'.soccer-scraper.json',{'format':1})
            atomic_json(root/'identity.json',{'worker_id':'msi','instance_id':'test'})
            atomic_json(root/'reporting.json',{'token':'test-private'})
            league=root/'premier-league/2021-2022/raw';league.mkdir(parents=True)
            organize_data_root(root)
            self.assertTrue(league.exists());self.assertFalse((root/'identity.json').exists())
            self.assertEqual(json.loads(system_path(root,'reporting.json').read_text())['token'],'test-private')
            self.assertEqual(organize_data_root(root),[])
    def test_layout_defers_if_old_worker_is_running(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);atomic_json(root/'identity.json',{'worker_id':'msi'})
            with FileLock(str(root/'.worker.lock')):
                with self.assertRaises(Timeout):organize_data_root(root)
            self.assertTrue((root/'identity.json').exists())
    def test_cleanup_keeps_unique_response_and_is_replayable(self):
        from tools.compact_cache import compact
        with TemporaryDirectory() as tmp:
            root=Path(tmp);atomic_json(root/'.soccer-scraper.json',{'format':1})
            p=root/'http_cache/fotmob/old.json'
            saved={'identity':json.dumps(['https://www.fotmob.com/api/data','matchDetails',{'matchId':7},'json']),'status':200,'payload':{'general':{'matchId':7}}}
            atomic_json(p,saved)
            preview=compact(root);self.assertTrue(p.exists());self.assertEqual(preview['recovered'],1)
            compact(root,apply=True);self.assertFalse(p.exists())
            self.assertEqual(RequestCache(root,'fotmob').read('matchDetails',{'matchId':7},'json')['payload'],saved['payload'])

if __name__=='__main__':unittest.main()

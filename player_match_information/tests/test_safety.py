import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from api.safeClient import PersistentGate, SafeSofaScoreClient
from api.httpClient import ProviderRequestError
from collection.seasonRunner import SeasonRunner
from test_providers import Sofa, event
from storage.pandasStore import atomic_json
from transfer import pack, restore


def response(status=200, payload=None, headers=None):
    r=Mock(status_code=status,headers=headers or {})
    r.json.return_value={} if payload is None else payload
    return r


class SafetyTests(unittest.TestCase):
    def test_sofascore_www_host_reuses_successful_legacy_http_cache_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Mock()
            with SafeSofaScoreClient(folder,'sofascore',offline=True,session=session) as client:
                identity=json.dumps(['https://api.sofascore.com/api/v1','player/7',None,'json'],sort_keys=True)
                path=client.response_directory/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
                atomic_json(path,{'identity':identity,'status':200,'payload':{'player':{'id':7}}})
                self.assertEqual(client.player(7),{'player':{'id':7}})
                session.get.assert_not_called()

    def test_old_host_404_does_not_prevent_a_request_to_www(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Mock();session.get.return_value=response(payload={'player':{'id':7}})
            with SafeSofaScoreClient(folder,'sofascore',session=session) as client:
                identity=json.dumps(['https://api.sofascore.com/api/v1','player/7',None,'json'],sort_keys=True)
                path=client.response_directory/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
                atomic_json(path,{'identity':identity,'status':404})
                self.assertEqual(client.player(7),{'player':{'id':7}})
                self.assertEqual(session.get.call_args.args[0],
                                 'https://www.sofascore.com/api/v1/player/7')

    def test_refusal_persists_across_restarts_and_retry_after_is_respected(self):
        for status in (401,403,429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                session=Mock();session.get.return_value=response(status,headers={'Retry-After':'172800'})
                with patch('api.safeClient.time.time',return_value=1000):
                    with SafeSofaScoreClient(folder,'sofascore',session=session) as client:
                        with self.assertRaises(ProviderRequestError):client.event(1)
                        self.assertGreaterEqual(client.request_gate.read()['blocked_until'],173800)
                    with SafeSofaScoreClient(folder,'sofascore',session=session) as restarted:
                        with self.assertRaises(ProviderRequestError):restarted.event(2)
                session.get.assert_called_once()

    def test_cache_and_negative_cache_make_no_repeat_requests(self):
        for status in (200,404):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                session=Mock();session.get.return_value=response(status,{'event':{'id':1}})
                for offline in (False,True):
                    with SafeSofaScoreClient(folder,'sofascore',session=session,offline=offline) as client:
                        if status==404:
                            with self.assertRaises(ProviderRequestError):client.event(1)
                        else:self.assertEqual(client.event(1),{'event':{'id':1}})
                session.get.assert_called_once()

    def test_offline_miss_never_touches_network(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Mock()
            with SafeSofaScoreClient(folder,'sofascore',session=session,offline=True) as client:
                with self.assertRaisesRegex(ProviderRequestError,'Offline cache miss'):client.event(1)
            session.get.assert_not_called()

    def test_legacy_batch_state_does_not_limit_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=PersistentGate(folder,'sofascore')
            atomic_json(gate.traffic_path,{
                'batch_count':1000,
                'rest_until':9999999999,
                'next_request':0,
            })
            with patch('api.safeClient.time.time',return_value=1000):
                gate.wait('next')
            traffic=json.loads(gate.traffic_path.read_text())
            self.assertNotIn('batch_count',traffic)
            self.assertNotIn('rest_until',traffic)
            self.assertGreaterEqual(traffic['next_request'],1008)
            self.assertLessEqual(traffic['next_request'],1015)

    def test_pacing_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=PersistentGate(folder,'sofascore')
            with patch('api.safeClient.time.time',side_effect=[1000,1000,1025]), patch('api.safeClient.time.sleep') as sleep:
                gate.wait('one')
                PersistentGate(folder,'sofascore').wait('two')
                self.assertGreaterEqual(sleep.call_args.args[0],8)
                self.assertLessEqual(sleep.call_args.args[0],15)

    def test_200_challenge_opens_circuit(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Mock();session.get.return_value=response()
            session.get.return_value.json.side_effect=ValueError('HTML challenge')
            with SafeSofaScoreClient(folder,'sofascore',session=session) as client:
                with self.assertRaises(ProviderRequestError):client.event(1)
                with self.assertRaises(ProviderRequestError):client.event(2)
                self.assertTrue(client.request_gate.is_blocked)
            session.get.assert_called_once()

    def test_502_is_match_local_and_does_not_disable_provider(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('api.safeClient.random.uniform',return_value=0):
            session=Mock();session.get.side_effect=[response(502),response(200,{'event':{'id':2}})]
            with SafeSofaScoreClient(folder,'sofascore',session=session) as client:
                with self.assertRaises(ProviderRequestError):client.event(1)
                self.assertFalse(client.request_gate.is_blocked)
                self.assertEqual(client.event(2),{'event':{'id':2}})
            self.assertEqual(session.get.call_count,2)

    def test_season_capability_deferral_is_persistent_and_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            sofa=Sofa();sofa.player_heatmap=Mock(side_effect=ProviderRequestError('missing',status_code=404))
            runner=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'])
            for mid in (1,2,3):
                self.assertEqual(runner._player_spatial_request(sofa,mid,7,'heatmap')[2],'unavailable')
            restarted=SeasonRunner({'sofascore':sofa},folder,providers=['sofascore'])
            self.assertEqual(restarted._player_spatial_request(sofa,4,7,'heatmap')[2],'deferred')
            self.assertEqual(sofa.player_heatmap.call_count,3)

    def test_transfer_preserves_cooldown_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'data';root.mkdir()
            atomic_json(root/'.soccer-scraper.json',{'format':1})
            atomic_json(root/'safety/sofascore.json',{'blocked_until':9999999999})
            archive=pack(root,Path(folder)/'state.zip')
            target=restore(archive,Path(folder)/'new-device')
            self.assertEqual((root/'safety/sofascore.json').read_bytes(),(target/'safety/sofascore.json').read_bytes())
            with self.assertRaisesRegex(ValueError,'must not exist'):restore(archive,target)



if __name__=='__main__':unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import yaml
import config
import api_test
from providers.providerProbe import probe_provider
from providers.safeClient import SafeWhoScoredClient, SafeFotMobClient
from providers.httpClient import ProviderError, ProviderRequestError


class ProviderProbeTests(unittest.TestCase):
    def test_real_clients_make_one_request_and_decode_their_responses(self):
        centre = {'events':[{'id':1}],
                  'home':{'name':'Home','players':[{'playerId':1}]},
                  'away':{'name':'Away','players':[{'playerId':2}]}}
        page = 'matchId:99, matchCentreData:'+json.dumps(centre)+', matchCentreEventTypeJson:{}'
        for provider,cls in [('whoscored',SafeWhoScoredClient),('fotmob',SafeFotMobClient)]:
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as tmp:
                session = Mock()
                response = Mock(status_code=200,text=page)
                response.json.return_value = {'general':{'matchId':'99'},
                    'header':{'teams':[{'name':'Home'},{'name':'Away'}]}}
                session.get.return_value = response
                with cls(tmp,provider,session=session) as client:
                    ok,message = probe_provider(client,provider,99)
                    self.assertTrue(ok,message)
                    self.assertIn('GOOD:',message)
                    self.assertFalse(client.refresh_cache)
                session.get.assert_called_once()

    def test_bad_response_does_not_report_success(self):
        for provider,payload in [('fotmob',{'general':{'matchId':'2'}}),
                                 ('whoscored',{'match_id':99,'matchCentreData':{'events':[]}}),
                                 ('fotmob',None)]:
            client=Mock(refresh_cache=False)
            client.match_details.return_value=payload
            ok,message=probe_provider(client,provider,99)
            self.assertFalse(ok)
            self.assertIn('NOT GOOD',message)
            self.assertFalse(client.refresh_cache)

    def test_provider_errors_and_cooldowns_restore_refresh(self):
        for error,expected in [(ProviderRequestError('HTTP 403',status_code=403,attempts=1),'NOT GOOD'),
                               (ProviderRequestError('cooldown',status_code=429),'NOT TESTED'),
                               (ProviderError('Missing event data'),'NOT GOOD')]:
            client=Mock(refresh_cache=False)
            client.match_details.side_effect=error
            ok,message=probe_provider(client,'whoscored',99)
            self.assertFalse(ok)
            self.assertIn(expected,message)
            client.match_details.assert_called_once_with(99)
            self.assertFalse(client.refresh_cache)

    def test_batch_continues_after_provider_failure(self):
        clients={p:MagicMock() for p in ('sofascore','whoscored','fotmob')}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(api_test.CLIENTS,clients), \
                patch.object(config,'API_TEST_PROVIDERS',list(clients)), \
                patch('api_test.probe_provider',side_effect=[(False,'NOT GOOD'),(True,'GOOD'),(True,'GOOD')]) as probe:
            self.assertEqual(api_test.main(['--data-root',tmp]),2)
            self.assertEqual(probe.call_count,3)
            self.assertEqual([call.args[1] for call in probe.call_args_list],list(clients))

    def test_single_provider_override_and_shared_data_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config,'DATA_ROOT',Path(tmp)), \
                patch.dict(api_test.CLIENTS,{'fotmob':MagicMock()}), \
                patch('api_test.probe_provider',return_value=(True,'GOOD')) as probe:
            self.assertEqual(api_test.main(['--providers','fotmob','--match-id','88']),0)
            self.assertEqual(probe.call_args.args[1:],('fotmob',88))
            api_test.CLIENTS['fotmob'].assert_called_once_with(Path(tmp).resolve(),'fotmob')

    def test_invalid_match_config_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.yaml'
            for value in (0,True,'123'):
                document=yaml.safe_load(config.CONFIG_FILE.read_text())
                document['api_test']['match_ids']['fotmob']=value
                path.write_text(yaml.safe_dump(document))
                with self.assertRaisesRegex(config.ConfigError,'positive integer'):
                    config.load_config(path)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml
import config
import whoscored_events
from collection.whoScoredEvents import main as collect
from providers.safeClient import PersistentGate, SafeWhoScoredClient


class WhoScoredConfigTests(unittest.TestCase):
    def test_launcher_routes_selection_and_ensemble_flags_through_main(self):
        options=['--competition','Bundesliga','--season','2024/25','--offline',
                 '--ensemble-root','shared','--worker-id','mac']
        with patch('whoscored_events.collect',return_value=0) as collect_mock:
            self.assertEqual(whoscored_events.main(options),0)
        collect_mock.assert_called_once_with([*options,'--collection-types','whoscored_events'])

    def test_configured_root_and_refresh_reach_the_runner(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(config,'DATA_ROOT',Path(folder)), \
                patch.object(config,'COMPETITIONS',['Bundesliga']), \
                patch.object(config,'SEASONS',['2024/25']), \
                patch.object(config,'WHOSCORED_REFRESH_SCHEDULE',True), \
                patch('collection.whoScoredEvents.create_whoscored_client'), \
                patch('collection.whoScoredEvents.WhoScoredEventRunner') as runner:
            runner.return_value.run.return_value=0
            self.assertEqual(collect([]),0)
            self.assertEqual(runner.call_args.args[1],Path(folder).resolve()/'bundesliga'/'2024-2025')
            runner.return_value.run.assert_called_once_with(
                'Bundesliga','2024/2025',limit=None,refresh_schedule=True)

    def test_yaml_rejects_invalid_pacing(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.yaml'
            for minimum,maximum in ((0,30),(-1,30),(40,30),(True,30),(15,float('inf'))):
                with self.subTest(minimum=minimum,maximum=maximum):
                    document=yaml.safe_load(config.CONFIG_FILE.read_text())
                    document['collection']['whoscored']['request_delay_min_seconds']=minimum
                    document['collection']['whoscored']['request_delay_max_seconds']=maximum
                    path.write_text(yaml.safe_dump(document))
                    with self.assertRaisesRegex(config.ConfigError,'request delays'):
                        config.load_config(path)

    def test_config_controls_gate_and_spacing_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(config,'WHOSCORED_MIN_DELAY',20), \
                patch.object(config,'WHOSCORED_MAX_DELAY',40):
            with SafeWhoScoredClient(folder,session=Mock()) as client:
                self.assertEqual(client.request_gate.min_delay,20)
                self.assertEqual(client.request_gate.max_delay,40)
                with patch('providers.safeClient.time.time',return_value=1000), \
                        patch('providers.safeClient.random.uniform',return_value=25) as jitter:
                    client.request_gate.wait('one')
                jitter.assert_called_once_with(20,40)
                with SafeWhoScoredClient(folder,session=Mock()) as restarted:
                    with patch('providers.safeClient.time.time',side_effect=[1001,1030]), \
                            patch('providers.safeClient.time.sleep') as sleep:
                        restarted.request_gate.wait('two')
                    sleep.assert_called_once_with(15)

    def test_switching_provider_keeps_whoscored_minimum_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=PersistentGate(folder,'whoscored',min_delay=30,max_delay=45)
            gate.traffic_path.write_text(json.dumps({'last_request':1000,'next_request':1008}))
            with patch('providers.safeClient.time.time',side_effect=[1010,1031]), \
                    patch('providers.safeClient.time.sleep') as sleep:
                gate.wait('match')
            sleep.assert_called_once_with(15)


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from collection.cli import main as collect
from providers.safeClient import SafeSofaScoreClient
from providers.sofaScoreProbe import probe_match


class MatchProbeTests(unittest.TestCase):
    def test_one_live_request_even_with_cached_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            session.get.return_value = Mock(status_code=200)
            session.get.return_value.json.return_value = {'event':{
                'id':123,'homeTeam':{'name':'Home'},'awayTeam':{'name':'Away'}}}
            with SafeSofaScoreClient(tmp,'sofascore',session=session) as client:
                client.event(123)
                session.get.reset_mock()
                with patch.object(client.request_gate,'wait'):
                    ok,message = probe_match(client,123)
                self.assertTrue(ok)
                self.assertIn('GOOD:',message)
                session.get.assert_called_once()
                self.assertFalse(client.refresh_cache)

    def test_refusal_reports_failure_and_retains_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            session.get.return_value = Mock(status_code=403,headers={})
            session.get.return_value.json.return_value = {}
            with SafeSofaScoreClient(tmp,'sofascore',session=session) as client:
                ok,message = probe_match(client,123)
                self.assertFalse(ok)
                self.assertIn('403',message)
                self.assertTrue(client.request_gate.is_blocked)
                session.get.assert_called_once()
                ok,message = probe_match(client,123)
                self.assertFalse(ok)
                self.assertIn('NOT TESTED',message)
                session.get.assert_called_once()

    def test_invalid_event_does_not_report_success(self):
        client = Mock(refresh_cache=False)
        client.event.return_value = {'event':{'id':999}}
        self.assertFalse(probe_match(client,123)[0])
        client.event.assert_called_once_with(123)

    def test_cli_probe_checks_a_game_instead_of_season_directory(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('collection.cli.SafeSofaScoreClient') as factory:
            client = factory.return_value.__enter__.return_value
            client.event.return_value = {'event':{'id':123,
                'homeTeam':{'name':'Home'},'awayTeam':{'name':'Away'}}}
            self.assertEqual(collect(['probe','--match-id','123','--data-root',tmp,'--sofascore-transport','http']),0)
            client.event.assert_called_once_with(123)
            client.seasons.assert_not_called()


if __name__ == '__main__':
    unittest.main()

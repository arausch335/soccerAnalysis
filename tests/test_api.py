import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

from curl_cffi import requests
from providers import (SofaScoreClient, SofaScoreError,
                 SofaScoreRequestError, SofaScoreLoader, RequestGate)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = SofaScoreClient(session=self.session, min_delay=0, max_delay=0, max_retries=3)
        self.sleep = patch('providers.sofaScoreClient.time.sleep').start()
        self.addCleanup(patch.stopall)

    def response(self, status=200, payload=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = {} if payload is None else payload
        return response

    def test_raw_data_params_and_timeout(self):
        payload = {'player': {'id': 7, 'futureField': 0}}
        self.session.get.return_value = self.response(payload=payload)
        self.assertEqual(self.client.get('/player/7', {'x': 1}), payload)
        self.session.get.assert_called_once_with(
            self.client.BASE_URL + '/player/7', params={'x': 1}, timeout=20)

    def test_transient_failures_then_success(self):
        self.session.get.side_effect = [
            self.response(503), requests.exceptions.Timeout('timed out'),
            self.response(payload={'ok': True})]
        self.assertEqual(self.client.event(10), {'ok': True})
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])

    def test_transport_failure_on_first_attempt(self):
        self.session.get.side_effect = [requests.exceptions.ConnectionError('offline'), self.response()]
        self.assertEqual(self.client.event(10), {})
        self.assertEqual(self.session.get.call_count, 2)

    def test_permanent_http_error_is_not_retried(self):
        for status in (400, 403, 404):
            with self.subTest(status=status):
                session = Mock()
                client = SofaScoreClient(session=session, min_delay=0, max_delay=0)
                session.get.return_value = self.response(status)
                with self.assertRaises(SofaScoreRequestError) as caught:
                    client.event(10)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.endpoint, 'event/10')
                self.assertEqual(caught.exception.attempts, 1)
                session.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_rate_limit_retry_after_and_exhaustion(self):
        self.session.get.return_value = self.response(429, headers={'Retry-After': '7'})
        with self.assertRaises(SofaScoreRequestError) as caught:
            self.client.event(10)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.attempts, 1)
        self.session.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_retry_after_http_date(self):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.session.get.side_effect = [
            self.response(503, headers={'Retry-After': format_datetime(deadline)}), self.response()]
        self.client.event(10)
        self.assertGreater(self.sleep.call_args.args[0], 28)
        self.assertLessEqual(self.sleep.call_args.args[0], 30)

    def test_invalid_json_and_wrong_shape_fail_clearly(self):
        response = self.response()
        response.json.side_effect = ValueError('invalid')
        self.session.get.return_value = response
        with self.assertRaisesRegex(SofaScoreRequestError, 'Invalid JSON'):
            self.client.event(10)
        self.session.get.assert_called_once()
        self.session.get.reset_mock()
        self.session.get.return_value = self.response(payload=[])
        with self.assertRaisesRegex(SofaScoreRequestError, 'JSON object'):
            self.client.event(10)
        self.session.get.assert_called_once()

    def test_exhausted_network_errors_keep_cause(self):
        cause = requests.exceptions.Timeout('offline')
        self.session.get.side_effect = cause
        with self.assertRaises(SofaScoreRequestError) as caught:
            self.client.event(10)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_rate_limit_includes_failed_attempt(self):
        client = SofaScoreClient(session=self.session, min_delay=3, max_delay=3, max_retries=3)
        self.session.get.side_effect = [requests.exceptions.Timeout('offline'), self.response()]
        with patch('providers.sofaScoreClient.time.monotonic', return_value=10):
            client.event(10)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 3])

    def test_shared_gate_paces_clients_and_opens_circuit_on_403(self):
        gate = RequestGate(3, 3)
        first_session, second_session = Mock(), Mock()
        first = SofaScoreClient(session=first_session, request_gate=gate)
        second = SofaScoreClient(session=second_session, request_gate=gate)
        first_session.get.return_value = self.response(payload={'ok': True})
        second_session.get.return_value = self.response(
            403, payload={'error': {'code': 403, 'reason': 'challenge'}})
        with patch('providers.sofaScoreClient.time.monotonic', return_value=10):
            self.assertEqual(first.event(1), {'ok': True})
            with self.assertRaisesRegex(SofaScoreRequestError, 'challenge'):
                second.event(2)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [3])
        first_session.get.reset_mock()
        with self.assertRaisesRegex(SofaScoreRequestError, 'circuit is open') as caught:
            first.event(3)
        self.assertEqual(caught.exception.attempts, 0)
        first_session.get.assert_not_called()

    def test_owned_and_injected_sessions(self):
        with patch('providers.sofaScoreClient.requests.Session') as factory:
            with SofaScoreClient() as client:
                self.assertIs(client.session, factory.return_value)
            client.close()
            factory.return_value.close.assert_called_once()
        self.client.close()
        self.session.close.assert_not_called()
        with self.assertRaisesRegex(SofaScoreError, 'closed'):
            self.client.event(1)

    def test_invalid_settings(self):
        for options in ({'max_retries': 0}, {'max_retries': 1.5}, {'timeout': 0},
                        {'timeout': float('nan')}, {'min_delay': 2, 'max_delay': 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                SofaScoreClient(session=self.session, **options)

    def test_paginated_schedule_is_lazy_and_deduplicated(self):
        self.session.get.side_effect = [
            self.response(payload={'events': [{'id': 2}, {'id': 1}], 'hasNextPage': True}),
            self.response(payload={'events': [{'id': 1}, {'id': 3}], 'hasNextPage': False}),
            self.response(payload={'events': [{'id': 3}, {'id': 4}], 'hasNextPage': False}),
        ]
        events = self.client.iter_season_events(17, 61627, include_upcoming=True)
        self.session.get.assert_not_called()
        self.assertEqual(next(events), {'id': 2})
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(list(events), [{'id': 1}, {'id': 3}, {'id': 4}])
        urls = [c.args[0] for c in self.session.get.call_args_list]
        self.assertTrue(urls[1].endswith('/events/last/1'))
        self.assertTrue(urls[2].endswith('/events/next/0'))

    def test_malformed_or_stalled_pagination_does_not_silently_truncate(self):
        self.session.get.return_value = self.response(payload={'events': [], 'hasNextPage': True})
        with self.assertRaisesRegex(SofaScoreError, 'no progress'):
            list(self.client.iter_season_events(17, 61627))
        self.session.get.return_value = self.response(payload={'events': []})
        with self.assertRaisesRegex(SofaScoreError, 'Invalid season'):
            list(self.client.iter_season_events(17, 61627))

    def test_missing_first_upcoming_page_completes_past_schedule(self):
        self.session.get.side_effect = [
            self.response(payload={'events': [{'id': 1}], 'hasNextPage': False}),
            self.response(404),
        ]
        self.assertEqual(list(self.client.iter_season_events(17, 61627, include_upcoming=True)), [{'id': 1}])
        self.assertEqual(self.session.get.call_count, 2)

    def test_other_schedule_failures_are_not_treated_as_completion(self):
        for direction, page, status in [('last', 0, 404), ('last', 1, 404),
                                        ('next', 1, 404), ('next', 0, 403),
                                        ('next', 0, 429), ('next', 0, 500)]:
            with self.subTest(direction=direction, page=page, status=status):
                def fetch(cid, sid, current_page, current_direction):
                    if (current_direction, current_page) == (direction, page):
                        raise SofaScoreRequestError('Schedule unavailable', status_code=status)
                    return {'events': [{'id': 1 if current_direction == 'last' else 2}],
                            'hasNextPage': current_direction == direction and current_page < page}
                with patch.object(self.client, 'season_events', side_effect=fetch):
                    with self.assertRaises(SofaScoreRequestError) as caught:
                        list(self.client.iter_season_events(17, 61627, include_upcoming=True))
                self.assertEqual(caught.exception.status_code, status)

    def test_loader_fetches_selected_season_in_chronological_order(self):
        def event(i):
            return {'id': i, 'startTimestamp': i * 100,
                    'homeTeam': {'id': 1, 'name': 'Home'},
                    'awayTeam': {'id': 2, 'name': 'Away'}}
        self.session.get.side_effect = [
            self.response(payload={'events': [event(2)], 'hasNextPage': True}),
            self.response(payload={'events': [event(1)], 'hasNextPage': False}),
            self.response(payload={'home': {'players': []}, 'away': {'players': []}}),
            self.response(payload={'home': {'players': []}, 'away': {'players': []}}),
        ]
        loader = SofaScoreLoader(self.client)
        season = loader.select_season(17, 'League', 61627, 'Season')
        matches = loader.iter_matches(season)
        self.assertEqual(next(matches).id, 1)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(next(matches).id, 2)


if __name__ == '__main__':
    unittest.main()

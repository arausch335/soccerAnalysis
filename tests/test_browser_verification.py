"""Human verification pauses the browser without creating a day-long ban."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, call

from providers.browserSofaScore import BrowserGate, BrowserSofaScoreClient
from providers.httpClient import ProviderRequestError
from providers.safeClient import PersistentGate
from tests.test_browser_transport import browser_settings
from tests.test_providers import event


def quiet(*args,**kwargs):pass


class BrowserVerificationTests(unittest.TestCase):
    def client(self,folder,text='Match details'):
        page=Mock(url='https://www.sofascore.com/football/match/home-away/abc#id:1')
        page.locator.return_value.inner_text.return_value=text
        client=BrowserSofaScoreClient(folder,browser_settings(),page=page,report=quiet)
        client._drive=Mock();client._diagnostic=Mock()
        return client,page

    def test_existing_browser_403_is_retired_but_http_403_is_preserved(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as folder:
                browser=PersistentGate(Path(folder)/'safety','sofascore_browser')
                http=PersistentGate(Path(folder)/'safety','sofascore')
                error=ProviderRequestError('HTTP 403',status_code=403)
                browser.block(error);http.block(error)
                if legacy:
                    state=browser.read();state.pop('status_code')
                    browser.path.write_text(json.dumps(state))
                gate=BrowserGate(folder,browser_settings())
                self.assertFalse(gate.is_blocked)
                gate.wait('next game')
                self.assertTrue(http.is_blocked)
                self.assertNotIn('blocked_until',gate.gate.read())

    def test_new_browser_403_does_not_block_the_next_action(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=BrowserGate(folder,browser_settings())
            gate.block(ProviderRequestError('HTTP 403',status_code=403))
            self.assertFalse(gate.is_blocked)
            gate.wait('next game')

    def test_browser_429_survives_restart_and_is_shared_with_http(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=BrowserGate(folder,browser_settings())
            error=ProviderRequestError('HTTP 429',status_code=429);error.retry_after=172800
            gate.block(error)
            restarted=BrowserGate(folder,browser_settings())
            restarted.block(ProviderRequestError('HTTP 403',status_code=403))
            self.assertTrue(restarted.is_blocked)
            self.assertTrue(PersistentGate(Path(folder)/'safety','sofascore').is_blocked)
            with self.assertRaises(ProviderRequestError) as caught:restarted.wait('next game')
            self.assertEqual(caught.exception.status_code,429)

    def test_captured_403_waits_for_human_then_retries_once(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder)
            client.responses[('event/1','')]={'status':403,'headers':{}}
            page.evaluate.return_value={'status':200,'payload':{'event':event(1)}}
            with patch('builtins.input',return_value='') as acknowledge:
                self.assertEqual(client.event(1),{'event':event(1)})
            acknowledge.assert_called_once();page.evaluate.assert_called_once()
            self.assertFalse(client.request_gate.is_blocked)

    def test_manual_completion_can_supply_a_native_response_without_an_extra_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder,'Verify you are human')
            client.responses[('event/1','')]={'status':403,'headers':{}}
            def complete(*args,**kwargs):
                client._save('event/1',{'event':event(1)},method='website_response')
            page.wait_for_function.side_effect=complete
            with patch('builtins.input',side_effect=AssertionError('Visible challenge needs no console acknowledgement')):
                self.assertEqual(client.event(1),{'event':event(1)})
            page.wait_for_function.assert_called_once()
            page.evaluate.assert_not_called()

    def test_fetch_403_requires_manual_completion_and_spaces_the_single_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder)
            page.evaluate.side_effect=[{'status':403,'payload':None},
                                       {'status':200,'payload':{'event':event(1)}}]
            with patch('builtins.input',return_value='') as acknowledge, \
                    patch.object(client.request_gate,'wait',wraps=client.request_gate.wait) as wait:
                self.assertEqual(client.event(1),{'event':event(1)})
            acknowledge.assert_called_once()
            self.assertEqual(page.evaluate.call_count,2)
            self.assertEqual(wait.call_args_list.count(call('browser resource: event/1')),2)

    def test_unresolved_403_stops_after_one_retry_and_can_resume_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder)
            page.evaluate.return_value={'status':403,'payload':None}
            with patch('builtins.input',return_value='') as acknowledge:
                with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,403)
            self.assertIn('No timed browser cooldown',str(caught.exception))
            self.assertEqual(page.evaluate.call_count,2);acknowledge.assert_called_once()
            self.assertFalse(client.request_gate.is_blocked)
            restarted,new_page=self.client(folder)
            new_page.evaluate.return_value={'status':200,'payload':{'event':event(1)}}
            self.assertEqual(restarted.event(1),{'event':event(1)})

    def test_verification_timeout_saves_no_cooldown_and_sends_no_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder,'Verifying you are a human')
            client.responses[('event/1','')]={'status':403,'headers':{}}
            page.wait_for_function.side_effect=TimeoutError('Not completed')
            with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,403)
            page.evaluate.assert_not_called()
            self.assertFalse(client.request_gate.is_blocked)

    def test_noninteractive_run_pauses_cleanly_for_manual_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder)
            client.responses[('event/1','')]={'status':403,'headers':{}}
            with patch('builtins.input',side_effect=EOFError):
                with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,403)
            page.evaluate.assert_not_called()
            self.assertFalse(client.request_gate.is_blocked)

    def test_429_during_verification_does_not_trigger_a_resource_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            client,page=self.client(folder,'Verify you are human')
            client.responses[('event/1','')]={'status':403,'headers':{}}
            def rate_limited(*args,**kwargs):
                client.responses[('event/1','')]={'status':429,'headers':{}}
            page.wait_for_function.side_effect=rate_limited
            with self.assertRaises(ProviderRequestError) as caught:client.event(1)
            self.assertEqual(caught.exception.status_code,429)
            page.evaluate.assert_not_called()
            self.assertTrue(client.request_gate.is_blocked)

    def test_clicks_and_their_retry_are_both_paced(self):
        with tempfile.TemporaryDirectory() as folder:
            client,_=self.client(folder);actions=Mock()
            client.request_gate=actions.gate;client._dismiss=Mock()
            actions.button.click.side_effect=[RuntimeError('late overlay'),None]
            client._click(actions.button)
            self.assertEqual(actions.mock_calls,[call.gate.wait('website click'),call.button.click(timeout=5000),
                call.gate.wait('website click retry'),call.button.click(timeout=5000)])
            close=client.page.get_by_role.return_value
            close.count.return_value=1;close.last.is_visible.return_value=True
            with patch.object(client,'_click') as click:client._close_player()
            click.assert_called_once_with(close.last)


if __name__=='__main__':unittest.main()

"""Optional full extension-to-Python regression on intercepted local site data."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import pandas as pd
from collection.playerStatsSchema import PLAYER_MATCH_COLUMNS
from collection.seasonRunner import SeasonRunner
from providers.extensionBridge import ExtensionBridge
from providers.extensionSofaScore import ExtensionSofaScoreClient
from tests.test_browser_transport import browser_settings
from tests.test_extension_client import FakeBridge
from tests.test_providers import event, lineup


def quiet(*args,**kwargs):pass


@unittest.skipUnless(os.environ.get('SOCCER_EXTENSION_UI_TESTS')=='1' and importlib.util.find_spec('playwright'),
                     'Set SOCCER_EXTENSION_UI_TESTS=1 and install optional Playwright/Chromium for the extension integration test')
class ExtensionUiTests(unittest.TestCase):
    def test_real_extension_connects_handles_manual_resume_and_exports_one_match(self):
        from playwright.sync_api import sync_playwright
        directory=Path(__file__).resolve().parents[1]/'chrome_extension'
        html='''<!doctype html><html><body>
        <div id="ad">Advertisement<button aria-label="Close ad" onclick="this.parentElement.remove()">Close ad</button></div>
        <button data-testid="tab-details">Overview</button><button data-testid="tab-lineups">Lineups</button>
        <button data-testid="tab-statistics">Statistics</button>
        <div onclick="show(7)"><div><img style="width:50px;height:50px" src="/api/v1/player/7/image"></div></div>
        <div onclick="show(8)"><div><img style="width:50px;height:50px" src="/api/v1/player/8/image"></div></div>
        <div data-testid="sheet" style="display:none"><button aria-label="Close" onclick="this.parentElement.style.display='none'">Close</button><span id="player"></span></div>
        <script>function show(id){document.querySelector('[data-testid=sheet]').style.display='block';document.querySelector('#player').textContent='Player '+id;}</script>
        </body></html>'''
        with tempfile.TemporaryDirectory() as folder,sync_playwright() as p:
            root=Path(folder);bridge=ExtensionBridge(report=quiet,connect_timeout=5);bridge.start()
            settings={**browser_settings(),'timeout_seconds':5,'manual_wait_seconds':10}
            context=p.chromium.launch_persistent_context(str(root/'profile'),headless=False,
                args=[f'--disable-extensions-except={directory}',f'--load-extension={directory}'])
            fake=FakeBridge();blocked=False;requests=[]
            def route(r):
                nonlocal blocked
                url=r.request.url
                if '/api/v1/' in url:
                    key=url.split('/api/v1/')[1];requests.append(key)
                    if key=='event/1/lineups' and not blocked:
                        blocked=True
                        r.fulfill(status=403,content_type='application/json',body='{}');return
                    result=fake.request('page',{'operation':'read','endpoint':key})
                    r.fulfill(status=result['status'],content_type='application/json',body=json.dumps(result.get('payload',{})))
                else:r.fulfill(status=200,content_type='text/html',body=html)
            context.route('https://www.sofascore.com/**',route)
            try:
                worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                extension_id=worker.url.split('/')[2]
                sofa=context.new_page();sofa.goto('https://www.sofascore.com/event/1')
                popup=context.new_page();popup.goto(f'chrome-extension://{extension_id}/popup.html')
                popup.locator('#code').fill(f'http://{bridge.address}/#{bridge.token}')
                # A browser-action popup leaves the site tab active. Reproduce
                # that state while inspecting the same popup as a test page.
                popup.evaluate('''async () => {
                    const [tab]=await chrome.tabs.query({url:'https://www.sofascore.com/*'});
                    await chrome.tabs.update(tab.id,{active:true});
                    document.querySelector('#connect').click();
                }''')
                connected_deadline=time.monotonic()+10
                while 'Connected.' not in popup.locator('#status').inner_text() and time.monotonic()<connected_deadline:
                    popup.wait_for_timeout(100)
                self.assertIn('Connected.',popup.locator('#status').inner_text())
                popup.locator('#pause').click()
                paused_deadline=time.monotonic()+5
                while not bridge.user_paused and time.monotonic()<paused_deadline:
                    popup.wait_for_timeout(100)
                self.assertTrue(bridge.user_paused)
                client=ExtensionSofaScoreClient(root,settings,bridge=bridge,report=quiet)
                runner=SeasonRunner({'sofascore':client},root/'season',providers=['sofascore'],
                    spatial=('average_positions',),report=quiet)
                with ThreadPoolExecutor() as pool:
                    future=pool.submit(runner.run,'EPL','2024/25')
                    popup.wait_for_timeout(300)
                    self.assertFalse(future.done())
                    popup.locator('#resume').click()
                    deadline=time.monotonic()+45;resumed=False
                    while not future.done() and time.monotonic()<deadline:
                        if 'Complete verification' in popup.locator('#status').inner_text():
                            popup.locator('#resume').click();resumed=True
                        popup.wait_for_timeout(100)
                    if not future.done():bridge.close()
                    result=future.result(timeout=3)
                self.assertTrue(resumed)
                self.assertEqual(result.status.tolist(),['complete'])
                frame=pd.read_csv(root/'season'/'player_match_stats.csv')
                self.assertEqual(tuple(frame.columns),PLAYER_MATCH_COLUMNS)
                self.assertEqual(len(frame),2)
                self.assertEqual(frame.passes_attempted.tolist(),[10,10])
                self.assertTrue(frame.ball_carries.isna().all())
                self.assertIn('event/1/player/7/statistics',requests)
                self.assertIn('event/1/player/8/statistics',requests)
                self.assertFalse(client.request_gate.is_blocked)
                popup.locator('#disconnect').click()
            finally:
                bridge.close();context.close()


if __name__=='__main__':unittest.main()

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
from providers.transports.bridge import ExtensionBridge
from providers.transports.sofascore_extension import ExtensionSofaScoreClient
from tools.extension_setup import prepare_extension
from tests.test_extension_payloads import extension_settings
from tests.test_extension_client import FakeBridge
from tests.test_providers import event, lineup


def quiet(*args,**kwargs):pass


@unittest.skipUnless(os.environ.get('SOCCER_EXTENSION_UI_TESTS')=='1' and importlib.util.find_spec('playwright'),
                     'Set SOCCER_EXTENSION_UI_TESTS=1 and install optional Playwright/Chromium for the extension integration test')
class ExtensionUiTests(unittest.TestCase):
    def test_real_extension_connects_handles_manual_resume_and_exports_one_match(self):
        from playwright.sync_api import sync_playwright
        html='''<!doctype html><html><body>
        <div id="ad">Advertisement<button aria-label="Close ad" onclick="this.parentElement.remove()">Close ad</button></div>
        <button data-testid="tab-details">Overview</button><button data-testid="tab-lineups">Lineups</button>
        <button data-testid="tab-statistics">Statistics</button>
        <div onclick="show(7)"><div><img style="width:50px;height:50px" src="/api/v1/player/7/image"></div></div>
        <div onclick="show(8)"><div><img style="width:50px;height:50px" src="/api/v1/player/8/image"></div></div>
        <div data-testid="sheet" style="display:none"><button aria-label="Close" onclick="this.parentElement.style.display='none'">Close</button><span id="player"></span></div>
        <script>
        function show(id){document.querySelector('[data-testid=sheet]').style.display='block';document.querySelector('#player').textContent='Player '+id;}
        function challengeClicked(){sessionStorage.setItem('challengeClicks',String(Number(sessionStorage.getItem('challengeClicks')||0)+1));}
        function automaticChallenge(){
          const content=document.body.innerHTML;
          document.body.innerHTML='<p>Verify you are human</p><button onclick="challengeClicked()">Verify</button>';
          setTimeout(()=>{history.replaceState(null,'',location.pathname);document.body.innerHTML=content;sessionStorage.setItem('automaticallyCleared','yes');},2000);
        }
        if(sessionStorage.getItem('needsReloadClearance')){
          sessionStorage.removeItem('needsReloadClearance');
          sessionStorage.setItem('reloadedForClearance','yes');automaticChallenge();
        }
        </script>
        </body></html>'''
        with tempfile.TemporaryDirectory() as folder,sync_playwright() as p:
            root=Path(folder);directory=prepare_extension(root/'load-this-extension')
            self.assertFalse(any(p.name.startswith('_') for p in directory.rglob('*')))
            bridge=ExtensionBridge(report=quiet,connect_timeout=5,diagnostics_dir=root/'trace');bridge.start()
            settings={**extension_settings(),'timeout_seconds':5,'manual_wait_seconds':10}
            context=p.chromium.launch_persistent_context(str(root/'profile'),headless=False,
                args=[f'--disable-extensions-except={directory}',f'--load-extension={directory}'])
            fake=FakeBridge();blocked=False;interrupted=False;requests=[]
            def route(r):
                nonlocal blocked,interrupted
                url=r.request.url
                if '/api/v1/' in url:
                    key=url.split('/api/v1/')[1];requests.append(key)
                    if key=='event/1/lineups' and not blocked:
                        blocked=True
                        sofa.evaluate("sessionStorage.setItem('needsReloadClearance','yes')")
                        r.fulfill(status=403,content_type='application/json',body='{}');return
                    if key=='event/1/player/8/statistics' and not interrupted:
                        interrupted=True;r.abort();return
                    result=fake.request('page',{'operation':'read','endpoint':key})
                    if '/events/' in key:
                        for fixture in result.get('payload',{}).get('events',[]):
                            fixture['customId']='abc'
                            fixture['homeTeam']['slug']='home';fixture['awayTeam']['slug']='away'
                    r.fulfill(status=result['status'],content_type='application/json',body=json.dumps(result.get('payload',{})))
                else:r.fulfill(status=200,content_type='text/html',body=html)
            context.route('https://www.sofascore.com/**',route)
            try:
                worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                extension_id=worker.url.split('/')[2]
                sofa=context.new_page();sofa.goto('https://www.sofascore.com/football/match/away-home/abc#id:1')
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
                    deadline=time.monotonic()+45;resumed=False;reload_scheduled=False
                    while not future.done() and time.monotonic()<deadline:
                        recovery=worker.evaluate('chrome.storage.session.get(null)')
                        if recovery.get('verification') and not reload_scheduled:
                            self.assertEqual(recovery['verificationReloads'],0)
                            self.assertGreater(recovery['verificationNextReloadAt'],time.time()*1000)
                            # Advance only the test deadline; production keeps its full minute.
                            worker.evaluate('''async () => {
                                await chrome.storage.session.set({verificationNextReloadAt:Date.now()-1});
                                await chrome.alarms.create('verification-reload',{when:Date.now()+1});
                            }''')
                            reload_scheduled=True
                        popup.wait_for_timeout(100)
                    if not future.done():bridge.close()
                    result=future.result(timeout=3)
                self.assertFalse(resumed)
                self.assertTrue(reload_scheduled)
                self.assertGreater(client.waiting_seconds,0)
                self.assertEqual(sofa.evaluate("sessionStorage.getItem('automaticallyCleared')"),'yes')
                self.assertEqual(sofa.evaluate("sessionStorage.getItem('reloadedForClearance')"),'yes')
                self.assertEqual(sofa.evaluate("Number(sessionStorage.getItem('challengeClicks')||0)"),0)
                trace=[json.loads(line) for line in bridge.diagnostics.path.read_text().splitlines()]
                self.assertEqual(result.status.tolist(),['complete'],
                    {'matches':result[['status','error']].to_dict('records'),
                     'trace':trace[-16:]})
                self.assertTrue(interrupted)
                frame=pd.read_csv(root/'season'/'player_match_stats.csv')
                self.assertEqual(tuple(frame.columns),PLAYER_MATCH_COLUMNS)
                self.assertEqual(len(frame),2)
                self.assertEqual(frame.passes_attempted.tolist(),[10,10])
                self.assertTrue(frame.ball_carries.isna().all())
                self.assertIn('event/1/player/7/statistics',requests)
                self.assertIn('event/1/player/8/statistics',requests)
                self.assertFalse(client.request_gate.is_blocked)
                self.assertTrue(any(row.get('result',{}).get('diagnostics',{}).get('recovery_attempts')==1 for row in trace))
                self.assertTrue(any(row.get('result',{}).get('diagnostics',{}).get('verification_resolution')=='automatic' for row in trace))
                self.assertTrue(any(row.get('result',{}).get('recovery_stage')=='reload_confirmed' for row in trace))
                self.assertTrue(any(row.get('result',{}).get('recovery_stage')=='confirmation_read' for row in trace))
                self.assertTrue(any(row.get('result',{}).get('recovery_stage')=='match_restore_requested' for row in trace))
                self.assertTrue(any(row.get('result',{}).get('recovery_stage')=='match_restored' for row in trace))
                self.assertIn('#id:1',sofa.url)
                self.assertNotIn(bridge.token,bridge.diagnostics.path.read_text())
                popup.locator('#disconnect').click()
            finally:
                bridge.close();context.close()


if __name__=='__main__':unittest.main()

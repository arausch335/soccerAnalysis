"""Real browser regression using entirely intercepted, deterministic website data."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from providers.browserSofaScore import BrowserSofaScoreClient
from providers.visibleStats import parse_visible_stats
from tests.test_browser_transport import browser_settings
from tests.test_providers import event,lineup

HTML='''<!doctype html><html><body>
<div id="ad" style="position:fixed;inset:0;z-index:9999;background:white">
Advertisement<button aria-label="Close ad" onclick="document.getElementById('ad').remove()">Close ad</button></div>
<button data-testid="tab-lineups" onclick="fetch('/api/v1/event/1/lineups')">Lineups</button>
<button data-testid="tab-statistics" onclick="fetch('/api/v1/event/1/statistics')">Statistics</button>
<button data-testid="tab-details">Overview</button>
<div onclick="showPlayer()" style="width:100px;height:100px"><div><img style="width:50px;height:50px" src="/api/v1/player/7/image"></div></div>
<div data-testid="sheet" style="display:none"><button aria-label="Close" onclick="this.parentElement.style.display='none'">Close</button>
Player One<br>Minutes played<br>90'<br>Touches<br>42</div>
<script>
fetch('/api/v1/event/1');fetch('/api/v1/event/1/lineups');
function showPlayer(){document.querySelector('[data-testid=sheet]').style.display='block';fetch('/api/v1/event/1/player/7/statistics');fetch('/api/v1/event/1/player/7/heatmap');}
</script></body></html>'''


class BrowserUiUnitTests(unittest.TestCase):
    def test_ad_host_matching_does_not_block_player_images_or_first_party(self):
        for url,blocked in [('https://ads.doubleclick.net/ad',True),
                            ('https://www.googleadservices.com/ad',True),
                            ('https://img.sofascore.com/api/v1/player/7/image',False),
                            ('https://www.sofascore.com/api/v1/event/1',False)]:
            route=Mock();route.request.url=url
            BrowserSofaScoreClient._ad_route(route)
            self.assertEqual(route.abort.called,blocked)
            self.assertEqual(route.continue_.called,not blocked)

    def test_rendered_values_do_not_infer_counts_from_percentages(self):
        values,labels=parse_visible_stats("Minutes played\n90'\nTouches\n42\nAccurate passes\n80%\nRuns out (succ.)\n3 (2)\nGoals prevented\n-0.77")
        self.assertEqual(values['minutesPlayed'],90)
        self.assertEqual(values['touches'],42)
        self.assertEqual(values['totalKeeperSweeper'],3)
        self.assertEqual(values['accurateKeeperSweeper'],2)
        self.assertEqual(values['goalsPrevented'],-.77)
        self.assertNotIn('totalPass',values)


@unittest.skipUnless(importlib.util.find_spec('playwright'),'Optional Playwright dependency is not installed')
class BrowserUiIntegrationTests(unittest.TestCase):
    def test_visible_chrome_dismisses_ad_and_captures_player_panel(self):
        from playwright.sync_api import sync_playwright
        data={'event/1':{'event':event(1)},'event/1/lineups':lineup(),
              'event/1/statistics':{'statistics':[]},
              'event/1/player/7/statistics':{'player':{'id':7},'statistics':{'minutesPlayed':90,'totalPass':42}},
              'event/1/player/7/heatmap':{'heatmap':[{'x':10,'y':20}]}}
        with tempfile.TemporaryDirectory() as tmp,sync_playwright() as p:
            try:context=p.chromium.launch_persistent_context(str(Path(tmp)/'profile'),channel='chrome',headless=False)
            except Exception as exc:
                if 'not found' in str(exc) or "doesn't exist" in str(exc):self.skipTest('Chrome is not installed')
                raise
            def route(r):
                url=r.request.url
                if '/api/v1/' in url:
                    key=url.split('/api/v1/')[1]
                    if key in data:r.fulfill(status=200,content_type='application/json',body=json.dumps(data[key]))
                    else:r.fulfill(status=404,content_type='application/json',body='{}')
                else:r.fulfill(status=200,content_type='text/html',body=HTML)
            context.route('**/*',route)
            page=context.pages[0]
            client=BrowserSofaScoreClient(tmp,browser_settings(),page=page,report=lambda *a,**k:None)
            requested=event(1);requested['customId']='NsU';client.register_event(requested)
            try:
                self.assertEqual(client.event(1)['event']['id'],1)
                self.assertEqual(page.locator('#ad').count(),0)
                self.assertEqual(client.lineups(1)['home']['players'][0]['player']['id'],7)
                result=client.player_event_statistics(1,7)
                self.assertEqual(result['statistics']['totalPass'],42)
                self.assertEqual(client.player_heatmap(1,7)['heatmap'][0]['x'],10)
                saved=json.loads(client._cache_path('event/1/player/7/statistics').read_text())
                self.assertEqual(saved['method'],'website_response')
            finally:context.close()


if __name__=='__main__':unittest.main()

"""WhoScored HTTP, visible browser and chosen-tab extension transports."""
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from storage.pandasStore import atomic_json
from .httpClient import ProviderError, ProviderRequestError
from .safeClient import SafeWhoScoredClient, PersistentGate
from .whoScoredClient import WhoScoredClient


class WhoScoredBrowserGate(PersistentGate):
    def __init__(self,root):
        import config
        self.root=Path(root)
        super().__init__(self.root/'safety','whoscored_browser',
            min_delay=config.WHOSCORED_MIN_DELAY,max_delay=config.WHOSCORED_MAX_DELAY)
        self.traffic_path=self.root/'safety'/'whoscored_browser_traffic.json'
        from filelock import FileLock
        self.lock=FileLock(str(self.traffic_path)+'.lock')

    def _shared_block(self):
        state=PersistentGate(self.root/'safety','whoscored').read()
        return state.get('status_code')==429 and state.get('blocked_until',0)>time.time()

    @property
    def is_blocked(self):return self._shared_block() or super().is_blocked

    def wait(self,endpoint):
        if self._shared_block():
            raise ProviderRequestError('WhoScored HTTP 429 waiting period also applies in Chrome',status_code=429)
        super().wait(endpoint)

    def block(self,error):
        # Verification is handled visibly, without recording a day-long browser ban.
        if error.status_code==403:return
        super().block(error)
        if error.status_code==429:PersistentGate(self.root/'safety','whoscored').block(error)


class BrowserWhoScoredClient(WhoScoredClient):
    def __init__(self,root,*,offline=False,mode='browser',page=None,bridge=None,report=print):
        import config
        self.root=Path(root);self.offline=offline;self.mode=mode;self.page=page;self.report=report
        self.settings=config.TRANSPORT['browser'];self._context=None;self._playwright=None
        self._closed=False;self.refresh_cache=False;self.network_requests=0;self.last_transport=mode
        self.request_gate=WhoScoredBrowserGate(root);self.bridge=bridge;self._owns_bridge=False;self._waiting_seconds=0.0
        self.cache=self.root/'http_cache'/'whoscored'

    @property
    def waiting_seconds(self):return self._waiting_seconds+getattr(self.bridge,'waiting_seconds',0.0)

    @property
    def requires_manual_attention(self):return getattr(self.bridge,'unavailable',False) is True

    def close(self):
        if self._closed:return
        try:
            if self._context is not None:self._context.close()
            if self._owns_bridge:self.bridge.close()
        finally:
            if self._playwright is not None:self._playwright.stop()
            self._closed=True

    def _start(self):
        if self._closed:raise ProviderError('WhoScored browser client is closed')
        if self.page is not None:return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError('WhoScored browser support needs: python -m pip install ".[browser]"') from exc
        self._playwright=sync_playwright().start()
        options=dict(headless=False,locale='en-US',viewport={'width':1440,'height':1000})
        if self.settings['channel']!='chromium':options['channel']=self.settings['channel']
        try:
            self._context=self._playwright.chromium.launch_persistent_context(
                str(self.root/'browser_profiles'/'whoscored'),**options)
            self.page=self._context.pages[0] if self._context.pages else self._context.new_page()
            self.page.set_default_timeout(self.settings['timeout_seconds']*1000)
        except Exception as exc:
            self.close()
            raise ProviderError('Could not open Chrome for WhoScored; check the installed browser/channel or use extension mode') from exc

    def _read(self,endpoint,params,kind):
        args={'endpoint':endpoint,'params':params or {},'kind':kind,
              'timeout_ms':self.settings['timeout_seconds']*1000,
              'manual_wait_ms':self.settings['manual_wait_seconds']*1000}
        if self.mode=='extension':
            if self.bridge is None:
                from .extensionBridge import acquire_bridge
                self.bridge,self._owns_bridge=acquire_bridge(report=self.report,
                    connect_timeout=self.settings['manual_wait_seconds'],
                    diagnostics_dir=self.root/'extension_diagnostics'/'whoscored')
            result=self.bridge.request('whoscored',args,
                timeout=self.settings['timeout_seconds']+self.settings['manual_wait_seconds']+15)
            self._waiting_seconds+=max(0,float(result.get('waiting_seconds',0)))
            return result
        self._start()
        try:
            url=self.BASE_URL+'/'+endpoint.lstrip('/')
            if kind=='text':
                response=self.page.goto(url,wait_until='domcontentloaded')
                status=response.status if response is not None else 200
            else:
                # Fixture JSON belongs to a stage, so enter that page first.
                stage=re.fullmatch(r'tournaments/(\d+)/data/',endpoint)
                if not stage:raise ValueError('Unknown WhoScored JSON endpoint')
                stage_url=self.BASE_URL+f'/stages/{stage[1]}/fixtures'
                if self.page.url.rstrip('/')!=stage_url:
                    self.page.goto(stage_url,wait_until='domcontentloaded')
                status=200
            source=Path(__file__).resolve().parents[1]/'chrome_extension'/'who_page.js'
            operation='('+source.read_text(encoding='utf-8').replace('export async function','async function',1)+')'
            deadline=time.monotonic()+self.settings['manual_wait_seconds']
            announced=False;waiting_start=None
            while True:
                result=self.page.evaluate(operation,args)
                if result.get('status')!=403 or not result.get('verification'):break
                if not announced:
                    self.report('WhoScored verification: inspect the visible Chrome window. Collection waits for you to finish verification.',flush=True)
                    announced=True;waiting_start=time.monotonic()
                if time.monotonic()>=deadline:
                    self._waiting_seconds+=time.monotonic()-waiting_start
                    return result
                self.page.wait_for_timeout(1000)
            if waiting_start is not None:self._waiting_seconds+=time.monotonic()-waiting_start
            if status in (401,404,429):result.update(status=status,ok=True)
            return result
        except ProviderError:raise
        except Exception as exc:
            raise ProviderRequestError('WhoScored browser page could not be read; resume the saved collection',endpoint=endpoint) from exc

    def _fetch(self,endpoint,params,kind):
        endpoint=endpoint.lstrip('/')
        if '..' in endpoint or '\\' in endpoint or not re.fullmatch(r'(?:matches/\d+/live|regions/\d+/tournaments/\d+/[a-zA-Z0-9-]+|regions/\d+/tournaments/\d+/seasons/\d+(?:/(?:stages/\d+/(?:show|fixtures)/)?[a-zA-Z0-9-]+)?|seasons/\d+/[^?#]+|tournaments/\d+/data/|stages/\d+/[^?#]+)',endpoint,re.I):
            raise ValueError('Endpoint is outside the WhoScored collection contract')
        if params and (kind!='json' or set(params)!={'d','isAggregate'} or not re.fullmatch(r'\d{6}',str(params['d'])) or params['isAggregate']!='false'):
            raise ValueError('Invalid WhoScored fixture parameters')
        identity=json.dumps([self.BASE_URL,endpoint,params,kind],sort_keys=True)
        path=self.cache/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
        match=re.fullmatch(r'matches/(\d+)/live',endpoint,re.I) if kind=='text' else None
        if path.exists() and not self.refresh_cache:
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved.get('identity')!=identity:raise ValueError('WhoScored browser cache identity mismatch')
            if saved.get('status')!=200:raise ProviderRequestError('Cached WhoScored response unavailable',status_code=saved.get('status'))
            try:
                if match:self.parse_match(saved['payload'],int(match[1]))
            except ProviderError:
                if self.offline:raise
            else:
                self.last_transport=saved.get('transport','http_cache');return saved['payload']
        if self.offline:raise ProviderRequestError('Offline WhoScored browser cache miss: '+endpoint,endpoint=endpoint)
        if self._closed:raise ProviderError('WhoScored browser client is closed')
        self.request_gate.wait(endpoint)
        result=self._read(endpoint,params,kind);self.network_requests+=1
        url=result.get('source_url','')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='www.whoscored.com' or parsed.username or parsed.password or parsed.port:
            raise ProviderError('WhoScored browser left the intended website')
        status=result.get('status')
        if status!=200:
            error=ProviderRequestError('WhoScored browser unavailable; finish verification and resume' if status==403 else f'WhoScored HTTP {status}',endpoint=endpoint,status_code=status)
            if status==429:
                try:error.retry_after=max(0,float(result.get('retryAfter') or 0))
                except ValueError:pass
            self.request_gate.block(error);raise error
        payload=result.get('text') if kind=='text' else result.get('payload')
        if not isinstance(payload,str if kind=='text' else dict):raise ProviderError('WhoScored browser returned an invalid response')
        if match:self.parse_match(payload,int(match[1]))
        atomic_json(path,dict(identity=identity,status=200,payload=payload,transport=self.mode,fetched_at=time.time()))
        self.last_transport=self.mode;return payload

    def get_text(self,endpoint,params=None):return self._fetch(endpoint,params,'text')
    def _get(self,endpoint,params=None):return self._fetch(endpoint,params,'json')


class WhoScoredTransport(WhoScoredClient):
    def __init__(self,root,provider='whoscored',*,offline=False,mode=None,http=None,browser=None,report=print):
        import config
        self.mode=mode or config.WHOSCORED_ACCESS
        if self.mode not in ('http','auto','browser','extension'):raise ValueError('Unknown WhoScored transport')
        self.offline=offline;self.refresh_cache=False;self.report=report
        self.http=http if http is not None else (SafeWhoScoredClient(root,provider,offline=offline) if self.mode in ('http','auto') else None)
        self.browser=browser if browser is not None else BrowserWhoScoredClient(root,offline=offline,mode='extension' if self.mode=='extension' else 'browser',report=report)
        self.active='http' if self.mode in ('http','auto') else self.mode;self._closed=False

    @property
    def network_requests(self):return sum(getattr(c,'network_requests',0) for c in (self.http,self.browser) if c is not None)
    @property
    def request_gate(self):return (self.http if self.active=='http' else self.browser).request_gate
    @property
    def waiting_seconds(self):return self.browser.waiting_seconds
    @property
    def requires_manual_attention(self):return self.browser.requires_manual_attention
    def close(self):
        try:self.browser.close()
        finally:
            if self.http is not None:self.http.close()
            self._closed=True

    def _call(self,method,*args):
        client=self.http if self.active=='http' else self.browser
        client.refresh_cache=self.refresh_cache
        try:return getattr(client,method)(*args)
        except (ProviderError,ValueError,UnicodeError) as exc:
            if self.mode!='auto' or self.active!='http' or self.offline or getattr(exc,'status_code',None) in (400,401,404,429):raise
            self.active='browser';self.browser.refresh_cache=True
            self.report('WhoScored HTTP unavailable; switching to visible Chrome for this run.',flush=True)
            return getattr(self.browser,method)(*args)

    def seasons(self,path):return self._call('seasons',path)
    def season_stages(self,path):return self._call('season_stages',path)
    def season_events(self,stage,month):return self._call('season_events',stage,month)
    def match_details(self,mid):return self._call('match_details',mid)
    def match_teams(self,mid):return self._call('match_teams',mid)


create_whoscored_client=WhoScoredTransport

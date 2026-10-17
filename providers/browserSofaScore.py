"""Visible Chrome transport, capturing the website's native Sofascore payloads."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, parse_qsl, urlencode, unquote

from storage.pandasStore import atomic_json
from .httpClient import ProviderError, ProviderRequestError, HttpClient
from .sofaScoreClient import SofaScoreClient

API_PATH=re.compile(r'/(?:api/v1/)(event/\d+(?:/.*)?|unique-tournament/\d+(?:/.*)?)$')
AD_HOSTS=('doubleclick.net','googlesyndication.com','googleadservices.com','adnxs.com','adsystem.com','outbrain.com','taboola.com','criteo.com','criteo.net','seedtag.com','missena.io')
CHALLENGE=re.compile(r'verify you are human|access denied|checking your browser|security verification',re.I)


def validate_payload(endpoint,payload):
    if not isinstance(payload,dict):raise ValueError('Expected a Sofascore JSON object')
    match=re.fullmatch(r'event/(\d+)',endpoint)
    if match and payload.get('event',{}).get('id')!=int(match[1]):
        raise ValueError('Sofascore browser returned a different match')
    if endpoint.endswith('/lineups'):
        ids=[]
        for side in ('home','away'):
            players=payload.get(side,{}).get('players')
            if not isinstance(players,list) or not players:raise ValueError('Both Sofascore lineups are required')
            ids.extend(p['player']['id'] for p in players)
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate Sofascore lineup player IDs')
    required=(('average-positions',('home','away')),('heatmap',('heatmap',)),
              ('shotmap',('shotmap',)),('statistics',('statistics',)),
              ('incidents',('incidents',)),('graph/momentum',('graphPoints',)))
    for suffix,fields in required:
        if endpoint.endswith('/'+suffix) and any(field not in payload for field in fields):
            raise ValueError(f'Sofascore browser response missing {fields}')
    if endpoint.endswith('/rating-breakdown') and not any(k in payload for k in ('passes','dribbles','carries','defending')):
        raise ValueError('Sofascore action maps missing')
    return payload


def event_url(event):
    """A schedule customId identifies the H2H page; the hash selects this event."""
    custom=event.get('customId')
    if custom and re.fullmatch(r'[A-Za-z0-9]+',str(custom)):
        slugs=[]
        for side in ('away','home'):
            team=event.get(side+'Team',{})
            slug=team.get('slug') or re.sub(r'[^a-z0-9]+','-',team.get('name',side).lower()).strip('-')
            slugs.append(slug)
        return f"https://www.sofascore.com/football/match/{'-'.join(slugs)}/{custom}#id:{int(event['id'])}"
    # The legacy numeric event route resolves the provider's canonical match URL.
    return f"https://www.sofascore.com/event/{int(event['id'])}"


class BrowserGate:
    """Short action spacing, with provider 429 waits shared with HTTP collection."""
    def __init__(self,root,settings):
        from .safeClient import PersistentGate
        self.root=Path(root);self.settings=settings
        self.gate=PersistentGate(self.root/'safety','sofascore_browser',
            min_delay=settings['delay_min_seconds'],max_delay=settings['delay_max_seconds'])
        # Browser actions have independent spacing, so an 8-15s HTTP slot is not
        # inherited. Explicit rate limits remain provider-wide.
        self.gate.traffic_path=self.root/'safety'/'browser_traffic.json'
        from filelock import FileLock
        self.gate.lock=FileLock(str(self.gate.traffic_path)+'.lock')

    def _shared(self):
        path=self.root/'safety'/'sofascore.json'
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if state.get('status_code')==429 or 'HTTP 429' in state.get('reason',''):
            return state.get('blocked_until',0)>time.time()
        return False

    @property
    def is_blocked(self):return self._shared() or self.gate.is_blocked

    def wait(self,endpoint):
        if self._shared():
            raise ProviderRequestError('Sofascore rate-limit waiting period applies to both transports',endpoint=endpoint,status_code=429)
        self.gate.wait(endpoint)

    def block(self,error):
        self.gate.block(error)
        if error.status_code==429:
            from .safeClient import PersistentGate
            PersistentGate(self.root/'safety','sofascore').block(error)


class BrowserSofaScoreClient(SofaScoreClient):
    """Same methods/IDs as the HTTP client; Playwright is imported only on launch."""
    BASE_URL='https://www.sofascore.com/api/v1'

    def __init__(self,root,settings,*,offline=False,page=None,report=print,proxy_env='SOCCER_PROXY_URL'):
        self.root=Path(root);self.settings=settings;self.offline=offline;self.report=report
        self.proxy_env=proxy_env;self.page=page;self._injected=page is not None
        self._playwright=None;self._context=None;self._closed=False
        self.refresh_cache=False;self.last_transport='browser';self.request_gate=BrowserGate(root,settings)
        self.responses={};self.events={};self.observations={};self.active_mid=None;self.active_player=None
        self.timeout=float(settings['timeout_seconds'])*1000
        self.cache_dir=self.root/'browser_cache'/'sofascore'
        self.diagnostics_dir=self.root/'browser_diagnostics'/'sofascore'
        if page is not None:page.on('response',self._capture)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        if self._closed:return
        try:
            if self._context is not None:self._context.close()
        finally:
            if self._playwright is not None:self._playwright.stop()
            self._closed=True

    def _start(self):
        if self._closed:raise ProviderError('Browser client is closed')
        if self.page is not None:return
        if self.offline:raise ProviderRequestError('Offline browser cache miss')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError('Visible browser support needs: python -m pip install ".[browser]"') from exc
        from .networkSettings import proxy_options
        proxy=proxy_options(self.proxy_env)
        options={'headless':False,'viewport':{'width':1440,'height':1000},'locale':'en-US'}
        if self.settings['channel']!='chromium':options['channel']=self.settings['channel']
        if proxy:
            parsed=urlsplit(proxy)
            options['proxy']={'server':f'{parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}'}
            if parsed.username:options['proxy']['username']=unquote(parsed.username)
            if parsed.password:options['proxy']['password']=unquote(parsed.password)
        self._playwright=sync_playwright().start()
        try:
            self._context=self._playwright.chromium.launch_persistent_context(
                str(self.root/'browser_profiles'/'sofascore'),**options)
            self.page=self._context.pages[0] if self._context.pages else self._context.new_page()
            self.page.set_default_timeout(self.timeout)
            self.page.on('response',self._capture)
            self.page.on('popup',lambda popup:popup.close())
            self.page.on('dialog',lambda dialog:dialog.dismiss())
            self._context.route('**/*',self._ad_route)
        except Exception as exc:
            self.close()
            raise ProviderError('Could not launch visible browser. Install Chrome, select msedge, or run python -m playwright install chromium.') from exc
        self.report('Opened visible Sofascore browser; using its dedicated saved profile.',flush=True)

    def _cache_path(self,endpoint,params=None):
        key=json.dumps([endpoint,params],sort_keys=True)
        return self.cache_dir/(hashlib.sha256(key.encode()).hexdigest()+'.json')

    def _save(self,endpoint,payload,*,method,params=None,status=200):
        record={'endpoint':endpoint,'params':params,'status':status,'transport':'browser',
            'method':method,'fetched_at':datetime.now(timezone.utc).isoformat(),
            'source_url':str(self.page.url) if self.page is not None else None,'payload':payload}
        atomic_json(self._cache_path(endpoint,params),record)
        self.responses[(endpoint,urlencode(params or {},doseq=True))]=record
        return payload

    def _capture(self,response):
        url=urlsplit(response.url)
        if url.hostname not in ('www.sofascore.com','api.sofascore.com'):return
        found=API_PATH.fullmatch(url.path)
        if not found:return
        endpoint=found[1]
        if response.request.resource_type not in ('xhr','fetch'):return
        # Ignore images/ads/country-IP lookup endpoints and never save cookies.
        key=(endpoint,url.query)
        try:
            status=response.status
            if status in (401,403,429):
                self.responses[key]={'status':status,'headers':response.headers}
                return
            payload=response.json()
            if status==404:
                self.responses[key]={'status':404,'payload':None}
                return
            if status==200:
                validate_payload(endpoint,payload)
                self._save(endpoint,payload,method='website_response',params=dict(parse_qsl(url.query)) or None)
        except Exception:
            # A malformed response never overwrites a valid cached payload.
            return

    def register_event(self,event):self.events[int(event['id'])]=deepcopy(event)

    @staticmethod
    def _ad_route(route):
        host=urlsplit(route.request.url).hostname or ''
        if any(host==ad or host.endswith('.'+ad) for ad in AD_HOSTS):route.abort()
        else:route.continue_()

    def _dismiss(self):
        # Match explicit controls only; never click an advertisement, download,
        # subscription offer, or a generic Continue/Agree button.
        for label in ('CONTINUE WITH LIMITED ACCESS','Reject all','Reject All',
                      'Close ad','Close advertisement','Dismiss ad','Skip ad',
                      'Close slide-in banner','Dismiss banner'):
            button=self.page.get_by_role('button',name=label,exact=True)
            if button.count() and button.first.is_visible():button.first.click(timeout=2500)
        for frame in self.page.frames:
            if frame==self.page.main_frame:continue
            host=urlsplit(frame.url).hostname or ''
            if not any(host==ad or host.endswith('.'+ad) for ad in AD_HOSTS):continue
            for label in ('Close ad','Close advertisement','Dismiss ad'):
                control=frame.get_by_role('button',name=label,exact=True)
                if control.count() and control.first.is_visible():control.first.click(timeout=1500)

    def _click(self,locator):
        self._dismiss()
        try:locator.click(timeout=min(self.timeout,5000))
        except Exception:
            # A late interstitial may have appeared after the initial check.
            self._dismiss()
            locator.click(timeout=min(self.timeout,5000))

    def _challenge(self):
        text=self.page.locator('body').inner_text(timeout=5000)
        if not CHALLENGE.search(text):return
        self.report('Sofascore needs manual attention in the visible browser; collection is waiting.',flush=True)
        try:
            self.page.wait_for_function("() => !/verify you are human|access denied|checking your browser|security verification/i.test(document.body?.innerText || '')",
                timeout=float(self.settings['manual_wait_seconds'])*1000)
        except Exception as exc:
            raise ProviderRequestError('Browser verification not completed; resume later',status_code=403) from exc

    def _navigate(self,url):
        self.request_gate.wait('website navigation');self._start()
        try:
            self.page.goto(url,wait_until='domcontentloaded',timeout=self.timeout)
            if urlsplit(self.page.url).hostname!='www.sofascore.com':
                raise ProviderError('Sofascore navigation left the expected website')
            self._dismiss();self._challenge()
        except ProviderError:raise
        except Exception as exc:
            raise ProviderRequestError('Sofascore browser navigation failed',attempts=1) from exc

    def _open_match(self,mid):
        if self.active_mid==mid:return
        self.active_player=None
        event=self.events.get(mid,{'id':mid})
        self._navigate(event_url(event));self.active_mid=mid
        self.report(f"Sofascore browser | match {mid} | {event.get('homeTeam',{}).get('name','?')} vs {event.get('awayTeam',{}).get('name','?')}",flush=True)

    def _close_player(self):
        close=self.page.get_by_role('button',name='Close',exact=True)
        if close.count() and close.last.is_visible():close.last.click(timeout=5000)
        self.active_player=None

    def _tab(self,name):
        # These stable data-testid attributes are exposed by the current site.
        if self.active_player is not None:self._close_player()
        locator=self.page.get_by_test_id('tab-'+name)
        try:locator.first.wait_for(state='visible',timeout=min(self.timeout,5000))
        except Exception:return False
        if locator.count() and locator.first.is_visible():
            self._click(locator.first);self._dismiss();return True
        return False

    def _open_player(self,mid,pid):
        if self.active_mid==mid and self.active_player==pid:return True
        self._open_match(mid);self._tab('lineups')
        images=self.page.locator(f'img[src*="/player/{pid}/image"]')
        try:images.first.wait_for(state='visible',timeout=min(self.timeout,5000))
        except Exception:return False
        if images.count():
            # Pitch images have an overlay; click the card container, not img.
            card=images.first.locator('xpath=../..')
            self._click(card)
            self.active_player=pid
            self.page.locator('[data-testid="sheet"]:visible, [data-testid="tab-extended"]:visible').first.wait_for(state='visible',timeout=min(self.timeout,5000))
            self.page.wait_for_timeout(200)
            return True
        return False

    def _drive(self,endpoint):
        match=re.match(r'event/(\d+)(?:/(.*))?$',endpoint)
        if match:
            mid=int(match[1]);suffix=match[2] or ''
            self._open_match(mid)
            player=re.match(r'player/(\d+)/',suffix)
            if player:
                self._open_player(mid,int(player[1]))
                if suffix.endswith('rating-breakdown'):
                    for category in ('pass','dribble','defensive','goalkeeping'):
                        control=self.page.get_by_test_id('tab-'+category)
                        if control.count() and control.first.is_visible():self._click(control.first)
            elif suffix in ('lineups','average-positions'):self._tab('lineups')
            elif suffix in ('statistics','shotmap'):self._tab('statistics')
            else:self._tab('details')
        elif self.page is None or not self.page.url.startswith('https://www.sofascore.com'):
            self._navigate('https://www.sofascore.com/')

    def _get(self,endpoint,params=None):
        endpoint=endpoint.lstrip('/')
        if not re.fullmatch(r'(event|unique-tournament|team|player|sport)/[a-zA-Z0-9_./-]+',endpoint) or '..' in endpoint:
            raise ValueError('Use a known Sofascore-relative endpoint')
        path=self._cache_path(endpoint,params)
        if path.exists() and not self.refresh_cache:
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved.get('endpoint')!=endpoint or saved.get('params')!=params:raise ValueError('Browser cache identity mismatch')
            if saved.get('status')==404:raise ProviderRequestError('Saved Sofascore browser HTTP 404',endpoint=endpoint,status_code=404)
            return validate_payload(endpoint,saved['payload'])
        if self.offline:raise ProviderRequestError('Offline browser cache miss: '+endpoint,endpoint=endpoint)
        self.request_gate.wait(endpoint);self._start()
        try:
            record=self.responses.get((endpoint,urlencode(params or {},doseq=True)))
            if record and record.get('status')==200 and not self.refresh_cache:
                return validate_payload(endpoint,record['payload'])
            try:self._drive(endpoint)
            except ProviderRequestError:raise
            except Exception:
                self._diagnostic(endpoint)
                # A late ad or changed control can block a click while the
                # first-party page session and its data resources still work.
                if not self.page.url.startswith('https://www.sofascore.com/'):raise
                self._challenge()
            self.page.wait_for_timeout(300)
            record=self.responses.get((endpoint,urlencode(params or {},doseq=True)))
            if record and record.get('status')==200:
                return validate_payload(endpoint,record['payload'])
            if record and record.get('status') in (401,403,429):
                if record['status']==403:self._challenge()
                self._fail_status(endpoint,record['status'],record.get('headers',{}))
            if record and record.get('status')==404:
                self._save(endpoint,None,params=params,method='website_response',status=404)
                raise ProviderRequestError('Sofascore website HTTP 404: '+endpoint,endpoint=endpoint,status_code=404)
            # Some fields load only on a specific UI toggle. Request the same
            # first-party resource from the loaded page with browser cookies.
            # This remains a browser fetch, not a separate HTTP session.
            if urlsplit(self.page.url).hostname!='www.sofascore.com':
                raise ProviderError('Browser left Sofascore; return to the match page before resuming')
            result=self.page.evaluate('''async ({path,timeout}) => {
                const controller=new AbortController();
                const timer=setTimeout(()=>controller.abort(),timeout);
                try {
                    const r=await fetch('/api/v1/'+path,{credentials:'same-origin',signal:controller.signal});
                    const text=await r.text();
                    let payload=null;try {payload=JSON.parse(text);} catch (_) {}
                    return {status:r.status,payload,retryAfter:r.headers.get('Retry-After')};
                } finally {clearTimeout(timer);}
            }''',{'path':endpoint+('?' + urlencode(params,doseq=True) if params else ''),'timeout':self.timeout})
            status=result['status']
            if status!=200:
                if status==404:self._save(endpoint,None,params=params,method='browser_fetch',status=404)
                self._fail_status(endpoint,status,{'Retry-After':result.get('retryAfter')})
            payload=validate_payload(endpoint,result['payload'])
            return self._save(endpoint,payload,params=params,method='browser_fetch')
        except ProviderError as exc:
            if getattr(exc,'status_code',None)!=404:self._diagnostic(endpoint)
            raise
        except Exception as exc:
            self._diagnostic(endpoint)
            raise ProviderRequestError(f'Browser data unavailable: {endpoint}; inspect browser_diagnostics',endpoint=endpoint,attempts=1) from exc

    def _fail_status(self,endpoint,status,headers=None):
        from types import SimpleNamespace
        error=ProviderRequestError(f'Sofascore browser HTTP {status}: {endpoint}',endpoint=endpoint,status_code=status,attempts=1)
        error.retry_after=HttpClient._retry_delay(SimpleNamespace(headers=headers or {}),0)
        if status in (401,403,429):self.request_gate.block(error)
        raise error

    def _diagnostic(self,endpoint):
        # Store only screenshots and rendered text from the Sofascore page.
        if self.page is None or urlsplit(self.page.url).hostname!='www.sofascore.com':return
        self.diagnostics_dir.mkdir(parents=True,exist_ok=True)
        name=hashlib.sha256(endpoint.encode()).hexdigest()[:16]
        try:
            self.page.screenshot(path=str(self.diagnostics_dir/(name+'.png')))
            (self.diagnostics_dir/(name+'.txt')).write_text(self.page.locator('body').inner_text(),encoding='utf-8')
        except Exception:pass

    def graph(self,mid):return self._get(f'event/{mid}/graph/momentum')

    def collect_details(self,event,lineups):
        """Capture every played player's stats plus match-level detail payloads."""
        self.register_event(event);mid=int(event['id']);result=deepcopy(lineups)
        coverage={};errors=[]
        for side in ('home','away'):
            for player in result[side]['players']:
                stats=player.get('statistics',{})
                if player.get('substitute',False) and not (stats.get('minutesPlayed') or 0)>0:continue
                pid=player['player']['id'];key=f'player/{pid}/statistics'
                try:
                    payload=self.player_event_statistics(mid,pid)
                    if payload.get('player',{}).get('id',pid)!=pid:
                        raise ValueError('Sofascore player detail ID mismatch')
                    expected_team=event[side+'Team']['id']
                    if payload.get('team',{}).get('id',expected_team)!=expected_team:
                        raise ValueError('Sofascore player detail team mismatch')
                    values=payload.get('statistics')
                    if not isinstance(values,dict):raise ValueError('Player statistics missing')
                    player['statistics']={**stats,**values};coverage[key]='complete'
                except (ProviderRequestError,ValueError) as exc:
                    if self.active_mid==mid and self.active_player==pid and self.page is not None:
                        from .visibleStats import parse_visible_stats
                        panel=self.page.locator('[data-testid="sheet"]:visible')
                        if not panel.count():
                            panel=self.page.get_by_test_id('tab-extended').locator('xpath=ancestor::div[.//button[@aria-label="Close"]][1]')
                        if panel.count():
                            visible,labels=parse_visible_stats(panel.first.inner_text())
                            if visible:
                                player['statistics']={**stats,**visible}
                                player['_visible_labels']=labels
                                coverage[key]='visible_only'
                                errors.append(f'{key}: visible fields saved; full structured data unavailable')
                    if key not in coverage:coverage[key]='unavailable' if getattr(exc,'status_code',None)==404 else 'failed'
                    if getattr(exc,'status_code',None)!=404:errors.append(str(exc))
                    if getattr(exc,'status_code',None) in (401,403,429):raise
        for key,request in (('statistics',lambda:self.statistics(mid)),('incidents',lambda:self.incidents(mid)),
                            ('momentum',lambda:self.graph(mid))):
            try:request();coverage[key]='complete'
            except ProviderRequestError as exc:
                coverage[key]='unavailable' if getattr(exc,'status_code',None)==404 else 'failed'
                if getattr(exc,'status_code',None)!=404:errors.append(str(exc))
                if getattr(exc,'status_code',None) in (401,403,429):raise
        self._save(f'event/{mid}/lineups',result,method='website_plus_player_statistics')
        self.observations[mid]={'transport':'browser','coverage':coverage,'errors':errors,
            'cache_directory':str(self.cache_dir.relative_to(self.root))}
        return result,self.observations[mid]

"""WhoScored API first, then the shared Chrome extension."""
from storage.layout import system_path
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from storage.pandasStore import atomic_json
from storage.requestCache import RequestCache
from ..httpClient import ProviderError, ProviderRequestError
from ..safeClient import SafeWhoScoredClient, PersistentGate
from ..whoScoredClient import WhoScoredClient


class WhoScoredExtensionGate(PersistentGate):
    def __init__(self,root):
        import config
        self.root=Path(root)
        super().__init__(system_path(self.root,'safety'),'whoscored_extension',
            min_delay=config.TRANSPORT['extension']['delay_min_seconds'],max_delay=config.TRANSPORT['extension']['delay_max_seconds'])
        self.traffic_path=system_path(self.root,'safety')/'extension_traffic.json'
        from filelock import FileLock
        self.lock=FileLock(str(self.traffic_path)+'.lock')

    def read(self):
        state=super().read()
        # Old extension versions persisted match-local timeouts for an hour.
        # Keep real authentication/rate-limit stops, but do not inherit those bans.
        code=state.get('status_code')
        return {} if code is not None and code not in (401,429) else state

    def _shared_block(self):
        state=PersistentGate(system_path(self.root,'safety'),'whoscored').read()
        return state.get('status_code')==429 and state.get('blocked_until',0)>time.time()

    @property
    def is_blocked(self):return self._shared_block() or super().is_blocked

    def wait(self,endpoint):
        if self._shared_block():
            raise ProviderRequestError('WhoScored HTTP 429 waiting period also applies in Chrome',status_code=429)
        super().wait(endpoint)

    def block(self,error):
        # Missing data, transient errors and local timeouts concern one request.
        # Verification still goes through the visible extension workflow.
        if error.status_code not in (401,429):return
        super().block(error)
        if error.status_code==429:PersistentGate(system_path(self.root,'safety'),'whoscored').block(error)


class ExtensionWhoScoredClient(WhoScoredClient):
    def __init__(self,root,*,offline=False,bridge=None,report=print):
        import config
        self.root=Path(root);self.offline=offline;self.report=report
        self.settings=config.TRANSPORT['extension']
        self._closed=False;self.refresh_cache=False;self.network_requests=0;self.last_transport='extension'
        self.request_gate=WhoScoredExtensionGate(root);self.bridge=bridge;self._owns_bridge=False;self._waiting_seconds=0.0
        self.cache=RequestCache(root,'whoscored')

    @property
    def waiting_seconds(self):return self._waiting_seconds+getattr(self.bridge,'waiting_seconds',0.0)

    @property
    def requires_manual_attention(self):return getattr(self.bridge,'unavailable',False) is True

    def close(self):
        if self._closed:return
        try:
            if self._owns_bridge:self.bridge.close()
        finally:
            self._closed=True

    def _read(self,endpoint,params,kind):
        args={'endpoint':endpoint,'params':params or {},'kind':kind,
              'timeout_ms':self.settings['timeout_seconds']*1000,
              'manual_wait_ms':self.settings['manual_wait_seconds']*1000}
        if kind=='json':
            stage=int(re.fullmatch(r'tournaments/(\d+)/data/',endpoint)[1])
            args['page_path']=self.fixture_paths[stage]
        if self.bridge is None:
            from .bridge import acquire_bridge
            self.bridge,self._owns_bridge=acquire_bridge(report=self.report,
                connect_timeout=self.settings['manual_wait_seconds'],
                diagnostics_dir=system_path(self.root,'extension_diagnostics')/'collector')
        result=self.bridge.request('whoscored',args,
            timeout=self.settings['timeout_seconds']+self.settings['manual_wait_seconds']+15)
        self._waiting_seconds+=max(0,float(result.get('waiting_seconds',0)))
        return result

    def _fetch(self,endpoint,params,kind):
        endpoint=endpoint.lstrip('/')
        if '..' in endpoint or '\\' in endpoint or not re.fullmatch(r'(?:|players/\d+/history|matches/\d+/live|regions/\d+/tournaments/\d+/[a-zA-Z0-9-]+|regions/\d+/tournaments/\d+/seasons/\d+(?:/(?:stages/\d+/(?:show|fixtures)/)?[a-zA-Z0-9-]+)?|seasons/\d+/[^?#]+|tournaments/\d+/data/|stages/\d+/[^?#]+)',endpoint,re.I):
            raise ValueError('Endpoint is outside the WhoScored collection contract')
        if params and (kind!='json' or set(params)!={'d','isAggregate'} or not re.fullmatch(r'\d{6}',str(params['d'])) or params['isAggregate']!='false'):
            raise ValueError('Invalid WhoScored fixture parameters')
        match=re.fullmatch(r'matches/(\d+)/live',endpoint,re.I) if kind=='text' else None
        saved=self.cache.read(endpoint,params,kind) if not self.refresh_cache else None
        if saved is not None:
            if saved['status']!=200:raise ProviderRequestError('Cached WhoScored response unavailable',status_code=saved['status'])
            match=re.fullmatch(r'matches/(\d+)/live',endpoint,re.I) if kind=='text' else None
            if match:self.parse_match(saved['payload'],int(match[1]))
            self.last_transport=saved.get('transport','extension')+'_cache';return saved['payload']
        if self.offline:raise ProviderRequestError('Offline WhoScored extension cache miss: '+endpoint,endpoint=endpoint)
        if self._closed:raise ProviderError('WhoScored extension client is closed')
        self.request_gate.wait(endpoint)
        result=self._read(endpoint,params,kind);self.network_requests+=1
        url=result.get('source_url','')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='www.whoscored.com' or parsed.username or parsed.password or parsed.port:
            raise ProviderError('WhoScored extension left the intended website')
        status=result.get('status')
        if result.get('reason')=='extension_timeout' or status==408:
            raise ProviderRequestError('WhoScored extension timed out waiting for data for this request; no provider cooldown applied',endpoint=endpoint,attempts=1)
        if result.get('reason')=='match_data_unavailable':
            raise ProviderRequestError('WhoScored reports no statistics for this match; other matches can continue',endpoint=endpoint,status_code=404,attempts=1)
        if status!=200:
            error=ProviderRequestError('WhoScored extension unavailable; finish verification and resume' if status==403 else f'WhoScored HTTP {status} for {endpoint}',endpoint=endpoint,status_code=status)
            if status==429:
                try:error.retry_after=max(0,float(result.get('retryAfter') or 0))
                except ValueError:pass
            self.request_gate.block(error);raise error
        payload=result.get('text') if kind=='text' else result.get('payload')
        if not isinstance(payload,str if kind=='text' else dict):raise ProviderError('WhoScored extension returned an invalid response')
        if match:self.parse_match(payload,int(match[1]))
        self.cache.write(endpoint,params,kind,dict(status=200,payload=payload,transport='extension'))
        self.last_transport='extension';return payload

    def get_text(self,endpoint,params=None):return self._fetch(endpoint,params,'text')
    def _get(self,endpoint,params=None):return self._fetch(endpoint,params,'json')

    def season_events(self,stage_id,year_month):
        if not re.fullmatch(r'\d{6}',year_month):raise ValueError('Use YYYYMM')
        # Stage IDs alone cannot construct a navigable WhoScored page. Resolve
        # their published routes lazily, including IDs restored from checkpoints.
        if stage_id not in getattr(self,'fixture_paths',{}):
            from collection.selection import season_key
            for path,season in reversed(getattr(self,'fixture_seasons',[])):
                found=[row for row in self.seasons(path) if season_key(row['name'])==season_key(season)]
                if len(found)!=1:continue
                self.season_stages(found[0]['path'])
                if stage_id in self.fixture_paths:break
        if stage_id not in getattr(self,'fixture_paths',{}):
            raise ProviderError(f'No published WhoScored fixture page for stage {stage_id}; refresh season discovery')
        return super().season_events(stage_id,year_month)


class WhoScoredTransport(WhoScoredClient):
    def __init__(self,root,provider='whoscored',*,offline=False,http=None,extension=None,report=print):
        import config
        self.offline=offline;self.refresh_cache=False;self.report=report
        self.http=http if http is not None else SafeWhoScoredClient(root,provider,offline=offline)
        self.extension=extension if extension is not None else ExtensionWhoScoredClient(root,offline=offline,report=report)
        self.active='http';self._closed=False

    @property
    def network_requests(self):return sum(getattr(c,'network_requests',0) for c in (self.http,self.extension) if c is not None)
    @property
    def request_gate(self):return (self.http if self.active=='http' else self.extension).request_gate
    @property
    def waiting_seconds(self):return self.extension.waiting_seconds
    @property
    def requires_manual_attention(self):return self.extension.requires_manual_attention
    def close(self):
        try:self.extension.close()
        finally:
            if self.http is not None:self.http.close()
            self._closed=True

    def _call(self,method,*args):
        client=self.http if self.active=='http' else self.extension
        client.refresh_cache=self.refresh_cache
        try:return getattr(client,method)(*args)
        except (ProviderError,ValueError,UnicodeError) as exc:
            status=getattr(exc,'status_code',None)
            schedule_method=method in ('seasons','season_stages','season_events','qualification_path','get_text','get')
            if self.active!='http' or self.offline or status in (400,429) or status==404 and not schedule_method:raise
            self.active='extension';self.extension.refresh_cache=True
            http_paths=getattr(self.http,'fixture_paths',{})
            ext_paths=getattr(self.extension,'fixture_paths',{})
            self.extension.fixture_paths={**(http_paths if isinstance(http_paths,dict) else {}),
                                          **(ext_paths if isinstance(ext_paths,dict) else {})}
            self.report('WhoScored HTTP unavailable; switching to the connected extension for this run.',flush=True)
            return getattr(self.extension,method)(*args)

    def get_text(self,endpoint,params=None):return self._call('get_text',endpoint,params)
    def register_fixture_season(self,path,season):
        self.http.register_fixture_season(path,season)
        self.extension.register_fixture_season(path,season)
    def get(self,endpoint,params=None):return self._call('get',endpoint,params)
    def qualification_path(self,path):return self._call('qualification_path',path)
    def seasons(self,path):return self._call('seasons',path)
    def season_stages(self,path):return self._call('season_stages',path)
    def season_events(self,stage,month):return self._call('season_events',stage,month)
    def match_details(self,mid):return self._call('match_details',mid)
    def match_teams(self,mid):return self._call('match_teams',mid)
    def player_profile(self,pid):
        result=self._call('player_profile',pid)
        if self.active!='http' or self.offline or (result.get('birth_date') and result.get('career')):return result
        # An HTTP page can contain the header before History's table is loaded.
        # Missing optional fields never justify discarding a validated birthday.
        self.active='extension';self.extension.refresh_cache=True
        self.report('WhoScored profile history is incomplete in HTTP; checking the connected extension.',flush=True)
        try:rendered=self.extension.player_profile(pid)
        except (ProviderError,ValueError,UnicodeError) as exc:
            self.report('WhoScored extension profile unavailable; retaining available HTTP profile evidence: '+str(exc),flush=True)
            return result
        for key in ('birth_date','club','career'):
            if not rendered.get(key) and result.get(key):rendered[key]=result[key]
        return rendered



create_whoscored_client=WhoScoredTransport

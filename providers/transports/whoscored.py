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
        # Verification is handled visibly, without recording a day-long browser ban.
        if error.status_code==403:return
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
        if '..' in endpoint or '\\' in endpoint or not re.fullmatch(r'(?:players/\d+/history|matches/\d+/live|regions/\d+/tournaments/\d+/[a-zA-Z0-9-]+|regions/\d+/tournaments/\d+/seasons/\d+(?:/(?:stages/\d+/(?:show|fixtures)/)?[a-zA-Z0-9-]+)?|seasons/\d+/[^?#]+|tournaments/\d+/data/|stages/\d+/[^?#]+)',endpoint,re.I):
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
        if status!=200:
            error=ProviderRequestError('WhoScored extension unavailable; finish verification and resume' if status==403 else f'WhoScored HTTP {status}',endpoint=endpoint,status_code=status)
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
            if self.active!='http' or self.offline or getattr(exc,'status_code',None) in (400,404,429):raise
            self.active='extension';self.extension.refresh_cache=True
            self.report('WhoScored HTTP unavailable; switching to the connected extension for this run.',flush=True)
            return getattr(self.extension,method)(*args)

    def seasons(self,path):return self._call('seasons',path)
    def season_stages(self,path):return self._call('season_stages',path)
    def season_events(self,stage,month):return self._call('season_events',stage,month)
    def match_details(self,mid):return self._call('match_details',mid)
    def match_teams(self,mid):return self._call('match_teams',mid)
    def player_profile(self,pid):return self._call('player_profile',pid)


create_whoscored_client=WhoScoredTransport

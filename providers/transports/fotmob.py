"""FotMob API with same-tab Chrome extension fallback."""
from storage.layout import system_path
from pathlib import Path
from filelock import FileLock
from urllib.parse import urlsplit
from ..fotMobClient import FotMobClient
from ..httpClient import ProviderError,ProviderRequestError
from ..safeClient import SafeFotMobClient,PersistentGate
from storage.requestCache import RequestCache

class ExtensionFotMobClient(FotMobClient):
    def __init__(self,root,*,offline=False,report=print,bridge=None):
        import config
        self.root=Path(root);self.offline=offline;self.report=report;self.bridge=bridge;self._owns_bridge=False
        self.settings=config.TRANSPORT['extension'];self.refresh_cache=False;self.network_requests=0;self._closed=False
        self.last_transport='extension';self.cache=RequestCache(root,'fotmob')
        self.request_gate=PersistentGate(system_path(self.root,'safety'),'fotmob_extension',min_delay=self.settings['delay_min_seconds'],max_delay=self.settings['delay_max_seconds'])
        self.request_gate.traffic_path=system_path(self.root,'safety')/'extension_traffic.json'
        self.request_gate.lock=FileLock(str(self.request_gate.traffic_path)+'.lock')
    @property
    def waiting_seconds(self):return getattr(self.bridge,'waiting_seconds',0.0)
    @property
    def requires_manual_attention(self):return getattr(self.bridge,'unavailable',False) is True
    def close(self):
        if not self._closed and self._owns_bridge:self.bridge.close()
        self._closed=True
    def _get(self,endpoint,params=None):
        if self._closed:raise ProviderError('FotMob extension client is closed')
        params=params or {}
        rules={'playerData':({'id'},{'id'}),'leagues':({'id','ccode3','season'},{'id'}),'matchDetails':({'matchId'},{'matchId'}),'matches':({'date'},{'date'})}
        allowed,required=rules.get(endpoint,(set(),{'invalid'}))
        if not required<=set(params) or set(params)-allowed:raise ValueError('Unknown FotMob extension endpoint/parameters')
        saved=self.cache.read(endpoint,params,'json') if not self.refresh_cache else None
        if saved is not None:
            if saved['status']!=200:raise ProviderRequestError('Saved FotMob HTTP '+str(saved['status']),status_code=saved['status'])
            self.last_transport=saved.get('transport','extension')+'_cache';return saved['payload']
        if self.offline:raise ProviderRequestError('Offline FotMob cache miss')
        # A genuine rate limit applies to API and extension traffic alike.
        state=PersistentGate(system_path(self.root,'safety'),'fotmob').read()
        import time
        if state.get('status_code')==429 and state.get('blocked_until',0)>time.time():
            raise ProviderRequestError('FotMob rate-limit wait applies to the extension',status_code=429)
        self.request_gate.wait(endpoint)
        if self.bridge is None:
            from .bridge import acquire_bridge
            self.bridge,self._owns_bridge=acquire_bridge(report=self.report,connect_timeout=self.settings['manual_wait_seconds'],diagnostics_dir=system_path(self.root,'extension_diagnostics')/'collector')
        result=self.bridge.request('fotmob',{'endpoint':endpoint,'params':params,'timeout_ms':self.settings['timeout_seconds']*1000,'manual_wait_ms':self.settings['manual_wait_seconds']*1000},timeout=self.settings['timeout_seconds']+self.settings['manual_wait_seconds']+15)
        self.network_requests+=1
        source=urlsplit(result.get('source_url',''))
        if source.scheme!='https' or source.netloc!='www.fotmob.com':raise ProviderError('FotMob extension left the intended website')
        status=result.get('status')
        if status!=200:
            error=ProviderRequestError('FotMob extension HTTP '+str(status),status_code=status,endpoint=endpoint)
            if status==429:PersistentGate(system_path(self.root,'safety'),'fotmob').block(error)
            raise error
        payload=result.get('payload')
        if not isinstance(payload,dict):raise ProviderError('Invalid FotMob extension response')
        self.cache.write(endpoint,params,'json',dict(status=200,payload=payload,transport='extension'))
        return payload

class FotMobTransport(FotMobClient):
    def __init__(self,root,provider='fotmob',*,offline=False,http=None,extension=None,report=print):
        self.http=http if http is not None else SafeFotMobClient(root,provider,offline=offline)
        self.extension=extension if extension is not None else ExtensionFotMobClient(root,offline=offline,report=report)
        self.active='http';self.offline=offline;self.refresh_cache=False;self.report=report;self._closed=False
    @property
    def network_requests(self):return self.http.network_requests+self.extension.network_requests
    @property
    def request_gate(self):return (self.http if self.active=='http' else self.extension).request_gate
    @property
    def waiting_seconds(self):return self.extension.waiting_seconds
    @property
    def requires_manual_attention(self):return self.extension.requires_manual_attention
    def close(self):
        try:self.extension.close()
        finally:self.http.close();self._closed=True
    def _call(self,method,*args):
        switched=False
        if self.active=='http':
            self.http.refresh_cache=self.refresh_cache
            try:return getattr(self.http,method)(*args)
            except (ProviderError,ValueError) as exc:
                if self.offline or getattr(exc,'status_code',None) in (400,404,429):raise
                self.active='extension';switched=True;self.report('FotMob API unavailable; switching to the connected extension for this run.',flush=True)
        self.extension.refresh_cache=self.refresh_cache or switched
        return getattr(self.extension,method)(*args)
    def _get(self,endpoint,params=None):return self._call('get',endpoint,params)
    def seasons(self,competition_id):return self._call('seasons',competition_id)
    def season_events(self,competition_id,season):return self._call('season_events',competition_id,season)
    def match_details(self,match_id):return self._call('match_details',match_id)
    def player_profile(self,pid):return self._call('player_profile',pid)
    def event(self,match_id):return self.match_details(match_id)
    def scheduled_events(self,date):return self._call('scheduled_events',date)

create_fotmob_client=FotMobTransport

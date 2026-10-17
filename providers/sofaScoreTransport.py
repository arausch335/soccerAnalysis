"""Select HTTP, visible browser, or a single sticky HTTP-to-browser fallback."""
from pathlib import Path
import json

from .browserSofaScore import BrowserSofaScoreClient, validate_payload
from .httpClient import ProviderError, ProviderRequestError
from .safeClient import SafeSofaScoreClient
from .sofaScoreClient import SofaScoreClient


class SofaScoreTransport(SofaScoreClient):
    def __init__(self,root,provider='sofascore',*,offline=False,mode=None,
                 http=None,browser=None,report=print):
        import config
        self.root=Path(root);self.offline=offline;self.refresh_cache=False;self.report=report
        self.mode=mode or config.TRANSPORT['sofascore_mode']
        if self.mode not in ('http','browser','auto'):raise ValueError('Unknown Sofascore transport')
        self.http=http if http is not None else (None if self.mode=='browser' else SafeSofaScoreClient(root,provider,offline=offline))
        self.browser=browser if browser is not None else BrowserSofaScoreClient(root,
            config.TRANSPORT['browser'],offline=offline,report=report,
            proxy_env=config.TRANSPORT['http']['proxy_env'])
        self.active='browser' if self.mode=='browser' else 'http'
        self.last_transport=self.active
        self.audit={};self.events={}

    @property
    def network_requests(self):
        counts=[getattr(c,'network_requests',None) for c in (self.http,self.browser) if c is not None]
        return sum(counts) if all(type(n) in (int,float) for n in counts) else None

    @property
    def uses_browser(self):return self.active=='browser'
    @property
    def request_gate(self):return (self.browser if self.uses_browser else self.http).request_gate
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        try:self.browser.close()
        finally:
            if self.http is not None:self.http.close()

    def register_event(self,event):
        self.events[int(event['id'])]=event
        self.browser.register_event(event)

    def _record(self,endpoint,transport,payload):
        self.last_transport=transport;self.audit[endpoint]={'transport':transport}
        match=payload.get('event') if isinstance(payload,dict) else None
        if isinstance(match,dict) and isinstance(match.get('id'),int):self.register_event(match)
        return payload

    def _get(self,endpoint,params=None):
        endpoint=endpoint.lstrip('/')
        if not self.uses_browser:
            self.http.refresh_cache=self.refresh_cache
            try:
                payload=self.http.get(endpoint,params)
                validate_payload(endpoint,payload)
                return self._record(endpoint,getattr(self.http,'last_transport','http'),payload)
            except (ProviderError,ValueError) as exc:
                status=getattr(exc,'status_code',None)
                if self.mode!='auto' or self.offline or status in (400,401,404,429):raise
                self.active='browser'
                self.report(f'HTTP collection unavailable at {endpoint}; switching to visible Sofascore browser for this run.',flush=True)
        self.browser.refresh_cache=self.refresh_cache
        payload=self.browser.get(endpoint,params)
        return self._record(endpoint,'browser',payload)

    def collect_details(self,event,lineups):
        self.register_event(event)
        return self.browser.collect_details(event,lineups)

    def match_payloads(self,mid):
        result={}
        for label,endpoint in (('statistics',f'event/{mid}/statistics'),('incidents',f'event/{mid}/incidents'),
                               ('momentum',f'event/{mid}/graph/momentum')):
            path=self.browser._cache_path(endpoint)
            if path.exists():
                saved=json.loads(path.read_text(encoding='utf-8'))
                if saved.get('endpoint')!=endpoint:raise ValueError('Browser detail cache identity mismatch')
                if saved.get('status')==200:result[label]=saved['payload']
        return result

    def graph(self,mid):
        return self._get(f'event/{mid}/graph/momentum') if self.uses_browser else super().graph(mid)


def create_sofascore_client(root,provider='sofascore',*,offline=False,mode=None):
    import config
    if (mode or config.TRANSPORT['sofascore_mode'])=='extension':
        from .extensionSofaScore import ExtensionSofaScoreClient
        return ExtensionSofaScoreClient(root,config.TRANSPORT['browser'],offline=offline)
    return SofaScoreTransport(root,provider,offline=offline,mode=mode)

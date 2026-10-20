"""Sofascore API first, followed by the connected Chrome extension."""
from pathlib import Path
from urllib.parse import urlencode
from .sofascore_payloads import SofaScoreExtensionPayloads, validate_payload
from .sofascore_extension import ExtensionSofaScoreClient
from ..httpClient import ProviderError
from ..safeClient import SafeSofaScoreClient
from ..sofaScoreClient import SofaScoreClient

class SofaScoreTransport(SofaScoreClient):
    captures_details=True
    def __init__(self,root,provider='sofascore',*,offline=False,http=None,extension=None,report=print):
        import config
        self.root=Path(root);self.offline=offline;self.refresh_cache=False;self.report=report
        self.http=http if http is not None else SafeSofaScoreClient(root,provider,offline=offline)
        self.extension=extension if extension is not None else ExtensionSofaScoreClient(root,config.TRANSPORT['extension'],offline=offline,report=report)
        self.active='http';self.last_transport='http';self.audit={};self.events={}
        self.responses=self.extension.responses;self.observations=self.extension.observations
        self.cache_dir=self.extension.cache_dir
        self.active_mid=None;self.active_player=None
    @property
    def network_requests(self):return sum(getattr(c,'network_requests',0) for c in (self.http,self.extension))
    @property
    def waiting_seconds(self):return self.extension.waiting_seconds
    @property
    def requires_manual_attention(self):return self.extension.requires_manual_attention
    @property
    def request_gate(self):return (self.extension if self.active=='extension' else self.http).request_gate
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        try:self.extension.close()
        finally:self.http.close()
    def register_event(self,event):
        self.events[int(event['id'])]=event;self.extension.register_event(event)
    def _get(self,endpoint,params=None):
        endpoint=endpoint.lstrip('/')
        switched=False
        if self.active=='http':
            self.http.refresh_cache=self.refresh_cache
            try:
                payload=validate_payload(endpoint,self.http.get(endpoint,params))
                self.last_transport=getattr(self.http,'last_transport','http')
            except (ProviderError,ValueError) as exc:
                if self.offline or getattr(exc,'status_code',None) in (400,404,429):raise
                self.active='extension';switched=True
                self.report(f'Sofascore API unavailable at {endpoint}; switching to the connected extension for this run.',flush=True)
            else:return self._record(endpoint,params,payload)
        self.extension.refresh_cache=self.refresh_cache or switched
        payload=self.extension.get(endpoint,params)
        self.last_transport=self.extension.last_transport
        return self._record(endpoint,params,payload)
    def _record(self,endpoint,params,payload):
        self.responses[(endpoint,urlencode(params or {},doseq=True))]={'status':200,'payload':payload}
        self.audit[endpoint]={'transport':self.last_transport}
        if endpoint.startswith('event/') and isinstance(payload.get('event'),dict):self.register_event(payload['event'])
        return payload
    def _save(self,endpoint,payload,*,method,params=None,status=200):
        record={'endpoint':endpoint,'params':params,'status':status,'transport':self.active,
                'method':method,'payload':payload}
        self.extension.cache.write(endpoint,params,'json',record)
        self.responses[(endpoint,urlencode(params or {},doseq=True))]=record
        return payload
    def _cached_response(self,*args,**kwargs):return self.extension._cached_response(*args,**kwargs)
    def collect_details(self,event,lineups):
        result,coverage=SofaScoreExtensionPayloads.collect_details(self,event,lineups)
        methods={entry['transport'] for endpoint,entry in self.audit.items() if endpoint.startswith(f"event/{event['id']}")}
        coverage['transport']='mixed' if len(methods)>1 else self.active
        return result,coverage
    def match_payloads(self,mid):return SofaScoreExtensionPayloads.match_payloads(self,mid)
    def graph(self,mid):return self._get(f'event/{mid}/graph/momentum')

create_sofascore_client=SofaScoreTransport

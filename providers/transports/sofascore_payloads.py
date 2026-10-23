"""Sofascore payload validation and extension detail coverage. No browser launcher."""
from storage.layout import system_path
from copy import deepcopy
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from storage.pandasStore import atomic_json
from ..httpClient import ProviderError, ProviderRequestError, HttpClient
from ..sofaScoreClient import SofaScoreClient
from storage.requestCache import RequestCache

def validate_payload(endpoint,payload):
    if not isinstance(payload,dict):raise ValueError('Expected a Sofascore JSON object')
    profile=re.fullmatch(r'player/(\d+)',endpoint)
    if profile and payload.get('player',{}).get('id')!=int(profile[1]):raise ValueError('Sofascore profile identity differs')
    if re.fullmatch(r'player/\d+/transfer-history',endpoint) and not isinstance(payload.get('transferHistory'),list):raise ValueError('Sofascore transfer history missing')
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


class ExtensionGate:
    """Short action spacing, with provider 429 waits shared with HTTP collection."""
    def __init__(self,root,settings):
        from ..safeClient import PersistentGate
        self.root=Path(root);self.settings=settings
        self.gate=PersistentGate(system_path(self.root,'safety'),'sofascore_extension',
            min_delay=settings['delay_min_seconds'],max_delay=settings['delay_max_seconds'])
        # Browser actions have independent spacing, so an 8-15s HTTP slot is not
        # inherited. Explicit rate limits remain provider-wide.
        self.gate.traffic_path=system_path(self.root,'safety')/'extension_traffic.json'
        from filelock import FileLock
        self.gate.lock=FileLock(str(self.gate.traffic_path)+'.lock')
        # Older browser runs persisted verification failures as day-long bans.
        # Retire only those 403 entries; retain rate limits and other failures.
        with self.gate.lock:
            state=self.gate.read()
            if (self.gate._blocked_status(state)==403 and
                    'HTTP 429' not in state.get('reason','')):
                for key in ('blocked_until','reason','status_code'):state.pop(key,None)
                atomic_json(self.gate.path,state)

    def _shared(self):
        path=system_path(self.root,'safety')/'sofascore.json'
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
        if error.status_code==403:return
        self.gate.block(error)
        if error.status_code==429:
            from ..safeClient import PersistentGate
            PersistentGate(system_path(self.root,'safety'),'sofascore').block(error)


class SofaScoreExtensionPayloads(SofaScoreClient):
    BASE_URL = 'https://www.sofascore.com/api/v1'
    def __init__(self,root,settings,*,offline=False,report=print):
        self.root=Path(root);self.settings=settings;self.offline=offline;self.report=report
        self._closed=False;self.network_requests=0;self.refresh_cache=False
        self.last_transport='extension';self.request_gate=ExtensionGate(root,settings)
        self.responses={};self.events={};self.observations={}
        self.active_mid=None;self.active_player=None
        self.timeout=float(settings['timeout_seconds'])*1000
        self.cache=RequestCache(root,'sofascore')
        self.cache_dir=self.cache.directory
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def register_event(self,event):self.events[int(event['id'])]=deepcopy(event)
    def _cached_response(self,endpoint,params=None):
        saved=self.cache.read(endpoint,params,'json')
        if saved is None:return None
        if saved['status']==404 and saved.get('transport')=='http':return None
        if saved['status']==404:raise ProviderRequestError('Saved Sofascore HTTP 404',endpoint=endpoint,status_code=404)
        payload=validate_payload(endpoint,saved['payload'])
        record={**saved,'payload':payload}
        self.responses[(endpoint,urlencode(params or {},doseq=True))]=record
        self.last_transport=saved.get('transport','extension')+'_cache'
        if hasattr(self,'audit'):self.audit[endpoint]={'transport':self.last_transport,'method':'saved_response'}
        return record
    def _fail_status(self,endpoint,status,headers=None):
        from types import SimpleNamespace
        error=ProviderRequestError(f'Sofascore browser HTTP {status}: {endpoint}',endpoint=endpoint,status_code=status,attempts=1)
        if status==403:
            error=ProviderRequestError(f'Sofascore browser verification/access still unresolved: {endpoint}; run again after manual verification. No timed browser cooldown was set.',endpoint=endpoint,status_code=403,attempts=1)
        error.retry_after=HttpClient._retry_delay(SimpleNamespace(headers=headers or {}),0)
        if status in (401,403,429):self.request_gate.block(error)
        raise error


    def graph(self,mid):return self._get(f'event/{mid}/graph/momentum')


    def match_payloads(self, mid):
        result = {}
        for label, suffix in (('statistics','statistics'),('incidents','incidents'),('momentum','graph/momentum')):
            endpoint = f'event/{mid}/{suffix}'
            saved = self.responses.get((endpoint, ''))
            if saved is None:
                try:saved = self._cached_response(endpoint)
                except (ProviderRequestError,ValueError) as exc:
                    if getattr(exc,'status_code',None) != 404 and label!='momentum':raise
            if saved is not None and saved.get('status') == 200:
                result[label] = saved['payload']
        return result


    def collect_details(self,event,lineups):
        """Capture every played player's stats plus match-level detail payloads."""
        self.register_event(event);mid=int(event['id']);result=deepcopy(lineups)
        coverage={};errors=[];skipped={};reasons={};blocked=False
        for side in ('home','away'):
            for player in result[side]['players']:
                key=f"player/{player['player']['id']}/statistics"
                if player.get('substitute',False) and not (player.get('statistics',{}).get('minutesPlayed') or 0)>0:
                    skipped[key]='not_played'
                else:coverage[key]='not_attempted'
        coverage.update({key:'not_attempted' for key in ('statistics','incidents','momentum')})
        for side in ('home','away'):
            for player in result[side]['players']:
                if blocked:break
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
                    reasons[key]=str(exc)
                    if coverage[key]!='visible_only':coverage[key]='unavailable' if getattr(exc,'status_code',None)==404 else 'failed'
                    if getattr(exc,'status_code',None)!=404:errors.append(f'{key}: {exc}')
                    blocked=(getattr(exc,'status_code',None) in (401,403,429) or
                             getattr(self,'requires_manual_attention',False) is True)
        for key,request in (('statistics',lambda:self.statistics(mid)),('incidents',lambda:self.incidents(mid)),
                            ('momentum',lambda:self.graph(mid))):
            if blocked:break
            try:request();coverage[key]='complete'
            except (ProviderRequestError,ValueError) as exc:
                coverage[key]='unavailable' if getattr(exc,'status_code',None)==404 else 'failed'
                reasons[key]=str(exc)
                if getattr(exc,'status_code',None)!=404:errors.append(f'{key}: {exc}')
                blocked=(getattr(exc,'status_code',None) in (401,403,429) or
                         getattr(self,'requires_manual_attention',False) is True)
        self._save(f'event/{mid}/lineups',result,method='website_plus_player_statistics')
        self.observations[mid]={'transport':'extension','coverage':coverage,'errors':errors,
            'skipped':skipped,'reasons':reasons,'blocked':blocked,
            'cache_directory':str(self.cache_dir.relative_to(self.root))}
        return result,self.observations[mid]

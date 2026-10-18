"""Existing Sofascore data contract over JavaScript in a user-selected Chrome tab."""
import json
import re
from urllib.parse import urlencode, urlsplit

from storage.pandasStore import atomic_json
from .browserSofaScore import BrowserSofaScoreClient, event_url, validate_payload
from .extensionBridge import acquire_bridge
from .httpClient import ProviderError, ProviderRequestError

ENDPOINT = re.compile(
    r'(?:event/\d+(?:/(?:lineups|statistics|incidents|graph/momentum|average-positions|shotmap|'
    r'player/\d+/(?:statistics|heatmap|rating-breakdown)))?|'
    r'unique-tournament/\d+/(?:seasons|season/\d+/events/(?:last|next)/\d+))$')


class ExtensionSofaScoreClient(BrowserSofaScoreClient):
    active = 'extension'
    uses_browser = True  # Request the same detailed coverage as visible-browser runs.

    def __init__(self, root, settings, *, offline=False, report=print, bridge=None):
        super().__init__(root, settings, offline=offline, report=report)
        self.cache_dir = self.root/'extension_cache'/'sofascore'
        self.bridge, self._owns_bridge = ((bridge, False) if bridge is not None else
            acquire_bridge(report=report, connect_timeout=settings['manual_wait_seconds'],
                           diagnostics_dir=self.root/'extension_diagnostics'/'sofascore'))
        self.last_transport = 'extension'
        self.audit = {}
        self.source_url = None

    def close(self):
        if not self._closed and self._owns_bridge:
            self.bridge.close()
        self._closed = True

    @property
    def requires_manual_attention(self):
        return getattr(self.bridge, 'unavailable', False) is True

    def _step(self, action, args, *, timeout=None):
        if self.requires_manual_attention:
            raise ProviderRequestError('Extension needs attention; reconnect and resume collection', attempts=0)
        self.request_gate.wait('extension '+action)
        # A transient read can reopen the match and retry once in Chrome.
        limit = (2*self.settings['timeout_seconds'] + 45 if action == 'page' else
                 max(40, self.settings['timeout_seconds'] + 20))
        result = self.bridge.request(action, args, timeout=timeout or limit)
        url = result.get('source_url')
        if url and (urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'www.sofascore.com' or
                    urlsplit(url).username or urlsplit(url).password or urlsplit(url).port):
            raise ProviderError('Extension left Sofascore; reconnect the intended tab')
        if url:
            self.source_url = url
        return result

    def _verify(self, endpoint, result):
        visible = result.get('diagnostics', {}).get('visible_challenge', False)
        reason = ('visible verification detected; complete it yourself in Chrome' if visible else
                  'a data request was denied; a visible verification challenge may not appear. Inspect the Chrome tab')
        self.report(f'Sofascore {endpoint}: {reason}, then click Resume in Soccer Collector to retry once. No 24-hour browser cooldown applies.', flush=True)
        self._step('verify', {'endpoint': endpoint, 'visible_challenge': visible,
            'challenge_evidence': result.get('challenge_evidence')}, timeout=self.settings['manual_wait_seconds'])
        # Manual navigation can change the selected page or open panel.
        self.active_mid = self.active_player = None

    def _prepare(self, endpoint):
        match = re.match(r'event/(\d+)(?:/(.*))?$', endpoint)
        if not match:
            return {'ok': True}
        mid = int(match[1]);suffix = match[2] or ''
        if self.active_mid != mid:
            self._step('navigate', {'url': event_url(self.events.get(mid, {'id': mid}))})
            self.active_mid = mid;self.active_player = None
        player = re.match(r'player/(\d+)/', suffix)
        pid = int(player[1]) if player else None
        tab = 'lineups' if player or suffix in ('lineups','average-positions') else (
            'statistics' if suffix in ('statistics','shotmap') else 'details')
        result = self._step('page', {'operation': 'prepare', 'tab': tab, 'player_id': pid, 'match_id': mid,
            'delay_min_seconds': self.settings['delay_min_seconds'],
            'delay_max_seconds': self.settings['delay_max_seconds']})
        if result.get('status') == 409:
            self._step('navigate', {'url': event_url(self.events.get(mid, {'id': mid}))})
            result = self._step('page', {'operation': 'prepare', 'tab': tab, 'player_id': pid, 'match_id': mid,
                'delay_min_seconds': self.settings['delay_min_seconds'],
                'delay_max_seconds': self.settings['delay_max_seconds']})
        if result.get('status') in (403, 409):
            if result['status'] == 409:
                raise ProviderRequestError('Selected tab shows another match; reconnect the intended page', status_code=409)
            return result
        self.active_player = pid
        return result

    def _read(self, endpoint, params):
        result = self._step('page', {'operation': 'read', 'endpoint': endpoint,
            'params': params, 'timeout_ms': self.timeout})
        if result.get('status') == 429:
            self._fail_status(endpoint, 429, {'Retry-After': result.get('retryAfter')})
        return result

    def _get(self, endpoint, params=None):
        endpoint = endpoint.lstrip('/')
        if not ENDPOINT.fullmatch(endpoint):
            raise ValueError('Endpoint is outside the extension collection contract')
        if params is not None and (not isinstance(params, dict) or params):
            raise ValueError('Extension endpoints do not accept query parameters')
        path = self._cache_path(endpoint, params)
        if path.exists() and not self.refresh_cache:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if saved.get('endpoint') != endpoint or saved.get('params') != params:
                raise ValueError('Extension cache identity mismatch')
            if saved.get('status') == 404:
                raise ProviderRequestError('Saved Sofascore extension HTTP 404', endpoint=endpoint, status_code=404)
            return validate_payload(endpoint, saved['payload'])
        if self.offline:
            raise ProviderRequestError('Offline extension cache miss: '+endpoint, endpoint=endpoint)
        if self._closed:
            raise ProviderError('Extension client is closed')
        prepared = self._prepare(endpoint)
        verified = prepared.get('status') == 403
        if verified:
            self._verify(endpoint, prepared)
            prepared = self._prepare(endpoint)
            if prepared.get('status') == 403:
                self._fail_status(endpoint, 403)
        result = self._read(endpoint, params)
        self.network_requests += 1
        if result.get('status') == 403 and not verified:
            self._verify(endpoint, result)
            prepared = self._prepare(endpoint)
            if prepared.get('status') == 403:
                self._fail_status(endpoint, 403)
            result = self._read(endpoint, params)
            self.network_requests += 1
        status = result.get('status')
        if status != 200:
            if status == 404:
                self._save(endpoint, None, params=params, status=404, method='extension_page_fetch')
            self._fail_status(endpoint, status, {'Retry-After': result.get('retryAfter')})
        payload = validate_payload(endpoint, result.get('payload'))
        return self._save(endpoint, payload, params=params, method='extension_page_fetch')

    def _save(self, endpoint, payload, *, method, params=None, status=200):
        record = {'endpoint': endpoint, 'params': params, 'status': status, 'transport': 'extension',
            'method': method, 'source_url': self.source_url, 'payload': payload}
        from datetime import datetime, timezone
        record['fetched_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(self._cache_path(endpoint, params), record)
        self.responses[(endpoint, urlencode(params or {}, doseq=True))] = record
        self.audit[endpoint] = {'transport': 'extension', 'method': method}
        return payload

    def collect_details(self, event, lineups):
        result, coverage = super().collect_details(event, lineups)
        coverage['transport'] = 'extension'
        return result, coverage

    def match_payloads(self, mid):
        result = {}
        for label, suffix in (('statistics','statistics'),('incidents','incidents'),('momentum','graph/momentum')):
            endpoint = f'event/{mid}/{suffix}'
            path = self._cache_path(endpoint)
            if path.exists():
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('endpoint') != endpoint:
                    raise ValueError('Extension detail cache identity mismatch')
                if saved.get('status') == 200:
                    result[label] = saved['payload']
        return result

    def graph(self, mid):
        return self._get(f'event/{mid}/graph/momentum')

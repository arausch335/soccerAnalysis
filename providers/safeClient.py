"""Disk-backed pacing, cooldowns and caches for portable collection."""
import hashlib
import json
import math
import random
import time
from pathlib import Path

from filelock import FileLock
from storage.pandasStore import atomic_json
from .httpClient import ProviderRequestError
from .sofaScoreClient import SofaScoreClient
from .fotMobClient import FotMobClient
from .whoScoredClient import WhoScoredClient


class PersistentGate:
    """One shared ledger per provider across seasons and process restarts.

    The CLI also holds a global run lock: no concurrent collectors. Times are
    UTC epoch values so copying the data directory preserves the cooldowns.
    """
    def __init__(self, directory, provider, *, min_delay=8, max_delay=15):
        if (any(isinstance(value,bool) or not isinstance(value,(int,float))
                or not math.isfinite(value) for value in (min_delay,max_delay))
                or not 0 < min_delay <= max_delay):
            raise ValueError('request delays must be finite and 0 < min_delay <= max_delay')
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.path = Path(directory) / (provider + '.json')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.traffic_path = Path(directory) / 'traffic.json'
        self.lock = FileLock(str(self.traffic_path) + '.lock')

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    @property
    def is_blocked(self):
        return self.read().get('blocked_until', 0) > time.time()

    def wait(self, endpoint):
        while True:
            with self.lock:
                state = self.read()
                now = time.time()
                if state.get('blocked_until', 0) > now:
                    raise ProviderRequestError(
                        f"Provider paused until {state['blocked_until']} UTC epoch: {state.get('reason')}",
                        endpoint=endpoint, status_code=429)
                traffic = json.loads(self.traffic_path.read_text(encoding='utf-8')) if self.traffic_path.exists() else {}
                # Global pacing also spaces requests when switching providers.
                # Apply this provider's spacing even after another provider
                # reserved a shorter delay. Older ledgers lack last_request.
                ready = max(traffic.get('next_request',0),
                            traffic.get('last_request',0) + self.min_delay)
                delay = ready - now
                if delay <= 0:
                    # Rewrite only the pacing timestamp. This also discards the
                    # retired batch_count/rest_until fields from older state.
                    atomic_json(
                        self.traffic_path,
                        {'last_request':now,
                         'next_request': now + random.uniform(self.min_delay, self.max_delay)},
                    )
                    return
            time.sleep(min(delay, 15))

    def block(self, error):
        seconds = 86400 if error.status_code in (401, 403, 429) else 3600
        seconds = max(seconds, getattr(error, 'retry_after', 0))
        with self.lock:
            state = self.read()
            state.update(blocked_until=max(state.get('blocked_until', 0), time.time() + seconds),
                         reason=str(error))
            atomic_json(self.path, state)


class CachedTransport:
    def __init__(self, state_directory, provider, *, offline=False, session=None,
                 min_delay=8, max_delay=15):
        self.offline = offline
        self.refresh_cache = False
        self.response_directory = Path(state_directory) / 'http_cache' / provider
        self.response_directory.mkdir(parents=True, exist_ok=True)
        gate = PersistentGate(Path(state_directory) / 'safety', provider,
                              min_delay=min_delay,max_delay=max_delay)
        super().__init__(request_gate=gate, max_retries=1, session=session)

    def _cached(self, endpoint, params, kind, request):
        identity = json.dumps([self.BASE_URL, endpoint, params, kind], sort_keys=True)
        path = self.response_directory / (hashlib.sha256(identity.encode()).hexdigest() + '.json')
        if path.exists() and not self.refresh_cache:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if saved['identity'] != identity:
                raise ValueError('HTTP cache identity mismatch')
            if saved.get('status') == 404:
                raise ProviderRequestError('Cached HTTP 404: ' + endpoint, endpoint=endpoint, status_code=404)
            return saved['payload']
        if self.offline:
            raise ProviderRequestError('Offline cache miss: ' + endpoint, endpoint=endpoint)
        try:
            payload = request()
        except ProviderRequestError as exc:
            if exc.status_code == 404:
                atomic_json(path, dict(identity=identity, status=404, fetched_at=time.time()))
            # A challenge can arrive as HTTP 200 HTML or invalid JSON. Stop
            # that provider rather than issuing the rest of a season's calls.
            elif exc.status_code == 200 or exc.status_code is None:
                self.request_gate.block(exc)
            raise
        atomic_json(path, dict(identity=identity, status=200, fetched_at=time.time(), payload=payload))
        return payload

    def _get(self, endpoint, params=None):
        return self._cached(endpoint, params, 'json', lambda: super(CachedTransport, self)._get(endpoint, params))

    def get_text(self, endpoint, params=None):
        def request():
            text = super(CachedTransport, self).get_text(endpoint, params)
            lower = text.lower()
            if any(marker in lower for marker in ('cf-chl-', 'verify you are human', 'captcha', 'access denied')):
                error = ProviderRequestError('Challenge page; collection paused', endpoint=endpoint, status_code=403)
                self.request_gate.block(error)
                raise error
            return text
        return self._cached(endpoint, params, 'text', request)


class SafeSofaScoreClient(CachedTransport, SofaScoreClient):
    pass


class SafeFotMobClient(CachedTransport, FotMobClient):
    pass


class SafeWhoScoredClient(CachedTransport, WhoScoredClient):
    def __init__(self, state_directory, provider='whoscored', *, offline=False, session=None):
        import config
        super().__init__(state_directory,provider,offline=offline,session=session,
                         min_delay=config.WHOSCORED_MIN_DELAY,max_delay=config.WHOSCORED_MAX_DELAY)

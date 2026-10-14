"""Small immutable response files rather than rewriting a growing cache CSV."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from storage.pandasStore import atomic_json


class ResponseCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, provider, key):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.directory / provider / f'{digest}.json'

    def fetch(self, provider, key, request, *, refresh=False):
        path = self.path(provider, key)
        if path.exists() and not refresh:
            saved = json.loads(path.read_text())
            if saved['key'] != key or saved['provider'] != provider:
                raise ValueError('Cache identity mismatch')
            return saved['payload']
        payload = request()
        atomic_json(path, {'provider':provider, 'key':key,
                          'fetched_at':datetime.now(timezone.utc).isoformat(), 'payload':payload})
        return payload

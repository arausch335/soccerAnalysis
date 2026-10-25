"""Compact, typed Reep ID bridges; no names or guessed links are imported.

Only teams and entities relevant to saved collections are retained. Bulk CSVs
are temporary. Human-reviewed identity decisions remain the stronger source.
"""
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
import csv
import gzip
import hashlib
import json
from pathlib import Path
import tempfile

from storage.pandasStore import atomic_json

PROVIDERS = ('sofascore', 'fotmob', 'whoscored')
KINDS = ('match', 'team', 'player')
# Reep preserves each provider's native route vocabulary. FotMob people use
# /players IDs, while Sofascore calls matches /event; coaches remain excluded.
NAMESPACES = {(kind, provider):{kind} for kind in KINDS for provider in PROVIDERS}
NAMESPACES['match', 'sofascore'] = {'event'}
NAMESPACES['player', 'fotmob'] = {'person', 'player'}
FORMAT = 'soccer-reep-index-v1'
LATEST = 'https://data.reep.football/releases/latest.json'


def path_for(root=None):
    from .identityReview import review_root
    return review_root(root) / '_system' / 'identity' / 'reep.json'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def install(root, value):
    if value.get('format') != FORMAT or value.get('version') != _digest(value.get('rows')):
        raise ValueError('Invalid Reep index or checksum')
    for row in value['rows']:
        if (len(row) != 4 or row[0] not in KINDS or row[1] not in PROVIDERS
                or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in row)):
            raise ValueError('Invalid typed Reep bridge')
    atomic_json(path_for(root), value)


@lru_cache(maxsize=8)
def _snapshot(path, mtime, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def snapshot(root=None):
    path = path_for(root)
    if not path.exists():
        return None
    stat = path.stat()
    return _snapshot(str(path), stat.st_mtime_ns, stat.st_size)


def version(root=None):
    value = snapshot(root)
    return value['version'] if value else ''


class Index:
    def __init__(self, value=None):
        self.metadata = {k: v for k, v in (value or {}).items() if k != 'rows'}
        self.native = defaultdict(set)
        self.canonical = defaultdict(set)
        for kind, provider, native, reep in (value or {}).get('rows', []):
            self.native[kind, provider, str(native)].add(reep)
            self.canonical[kind, reep, provider].add(str(native))

    def ids(self, kind, provider, native):
        return self.native.get((kind, provider, str(native)), set())

    def canonical_id(self, value):
        """Follow published redirects retained for relevant canonical entities."""
        redirects=self.metadata.get('redirects',{});seen=set()
        while value in redirects:
            if value in seen:raise ValueError('Cyclic Reep redirect')
            seen.add(value);value=redirects[value]
        return value

    def same(self, kind, left_provider, left, right_provider, right):
        a = self.ids(kind, left_provider, left)
        b = self.ids(kind, right_provider, right)
        if len(a) == len(b) == 1:
            return a == b
        return None

    def resolve(self, kind, row):
        """Fill only unique bridges; expose conflicts without overwriting IDs."""
        result = dict(row)
        mapped = set()
        ambiguous = False
        for provider in PROVIDERS:
            native = str(row.get(provider + '_id') or '')
            links = self.ids(kind, provider, native)
            ambiguous |= len(links) > 1
            mapped.update(links)
        result['reep_id'] = ''
        result['identity_status'] = 'unmapped'
        if ambiguous or len(mapped) > 1:
            result['identity_status'] = 'conflict'
        elif len(mapped) == 1:
            reep = next(iter(mapped))
            result['reep_id'] = reep
            result['identity_status'] = 'linked'
            for provider in PROVIDERS:
                candidates = self.canonical.get((kind, reep, provider), set())
                if not result.get(provider + '_id') and len(candidates) == 1:
                    result[provider + '_id'] = next(iter(candidates))
            if any(result.get(p + '_id') and self.ids(kind, p, result[p + '_id']) != {reep}
                   for p in PROVIDERS):
                result['identity_status'] = 'partly_verified'
        return result


@lru_cache(maxsize=8)
def _index(path, mtime, size):
    return Index(_snapshot(path, mtime, size))


def index(root=None):
    path = path_for(root)
    if not path.exists():
        return Index()
    stat = path.stat()
    return _index(str(path), stat.st_mtime_ns, stat.st_size)


def wanted_ids(root, dataset, ensemble=None):
    """Use small ID/checkpoint files, never traverse the large raw responses."""
    wanted = set()
    root, dataset = Path(root), Path(dataset)
    # The historical fixture builder discovers IDs before match details exist.
    # Retain their Reep peers on the next refresh as well.
    fixtures = dataset / 'fixtures.csv'
    if fixtures.exists():
        with fixtures.open(encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                for provider in PROVIDERS:
                    for entity, kind in (('match','match'), ('home_team','team'), ('away_team','team')):
                        native = row.get(f'{provider}_{entity}_id')
                        if native:
                            wanted.add((kind, provider, native))
    for folder in (dataset, dataset / 'concordance', dataset / 'fixture_concordance'):
        for kind in KINDS:
            path = folder / (kind + '_ids.csv')
            if not path.exists():
                continue
            with path.open(encoding='utf-8', newline='') as stream:
                for row in csv.DictReader(stream):
                    for provider in PROVIDERS:
                        if row.get(provider + '_id'):
                            wanted.add((kind, provider, row[provider + '_id']))
    from collection.paths import discover_collections
    for source in discover_collections(root, 'config.json'):
        cfg = json.loads((source / 'config.json').read_text(encoding='utf-8'))
        provider = cfg.get('id_namespace', cfg.get('baseline_provider', 'sofascore'))
        if provider not in PROVIDERS:
            continue
        schedule = source / 'schedule.json'
        if schedule.exists():
            for event in json.loads(schedule.read_text(encoding='utf-8')).get('events', []):
                wanted.add(('match', provider, str(event['id'])))
                for side in ('home', 'away'):
                    team = event.get(side + 'Team') or {}
                    if team.get('id') is not None:
                        wanted.add(('team', provider, str(team['id'])))
        for filename in ('player_match_stats.csv', 'identities.csv'):
            path = source / filename
            if not path.exists():
                continue
            with path.open(encoding='utf-8', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row.get('player_id'):
                        wanted.add(('player', provider, row['player_id']))
                    if row.get('provider') in PROVIDERS and row.get('provider_player_id'):
                        wanted.add(('player', row['provider'], row['provider_player_id']))
    if ensemble:
        for path in (Path(ensemble) / 'reporting' / 'workers').glob('*.json'):
            for record in json.loads(path.read_text(encoding='utf-8')).get('records', {}).values():
                for provider, native in (record.get('provider_match_ids') or {}).items():
                    if provider in PROVIDERS and native is not None:
                        wanted.add(('match', provider, str(native)))
    return wanted


def import_release(bridges, redirects, wanted, *, release, source, checksums):
    """Two streaming passes retain peers of wanted IDs and all team bridges."""
    for name, path in (('bridges', bridges), ('redirects', redirects)):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != checksums['csv/' + name + '.csv.gz']:
            raise ValueError('Reep checksum mismatch: ' + name)
    redirect = {}
    with gzip.open(redirects, 'rt', encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            old, new = row['from_id'], row['to_id']
            if old in redirect and redirect[old] != new:
                raise ValueError('Conflicting Reep redirects: ' + old)
            redirect[old] = new

    @lru_cache(maxsize=None)
    def canonical(value):
        seen = set()
        while value in redirect:
            if value in seen:
                raise ValueError('Cyclic Reep redirect')
            seen.add(value)
            value = redirect[value]
        return value

    def rows():
        with gzip.open(bridges, 'rt', encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                kind = row['entity_type']
                if row['namespace'] in NAMESPACES.get((kind, row['provider']), set()):
                    yield kind, row['provider'], row['external_id'], canonical(row['reep_id'])

    selected = set()
    for kind, provider, native, reep in rows():
        if kind == 'team' or (kind, provider, native) in wanted:
            selected.add((kind, reep))
    kept = sorted(set(row for row in rows() if (row[0], row[3]) in selected))
    canonical_ids={row[3] for row in kept}
    return {'format': FORMAT, 'version': _digest(kept), 'release': release,
            'source': source, 'license': 'CC0-1.0', 'checksums': checksums, 'rows': kept,
            'redirects':{old:canonical(old) for old in redirect if canonical(old) in canonical_ids}}


def refresh(root, dataset, ensemble=None):
    """Download verified public bulk files, then discard them after compaction."""
    from curl_cffi import requests
    pointer = requests.get(LATEST, impersonate='chrome', timeout=60)
    pointer.raise_for_status()
    release = pointer.json()['release_tag']
    if not all(c.isalnum() or c in '.-_' for c in release):
        raise ValueError('Invalid Reep release')
    base = 'https://data.reep.football/releases/' + release + '/'
    response = requests.get(base + 'checksums.txt', impersonate='chrome', timeout=60)
    response.raise_for_status()
    checksums = {line.split()[1]: line.split()[0] for line in response.text.splitlines() if line.strip()}
    wanted = wanted_ids(root, dataset, ensemble)
    with tempfile.TemporaryDirectory(prefix='soccer-reep-') as directory:
        paths = {}
        for name in ('redirects', 'bridges'):
            print('Downloading Reep ' + name + ' (temporary bulk file)...', flush=True)
            paths[name] = Path(directory) / (name + '.csv.gz')
            with closing(requests.get(base + 'csv/' + name + '.csv.gz', impersonate='chrome',
                                      timeout=600, stream=True)) as response:
                response.raise_for_status()
                with paths[name].open('wb') as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        stream.write(chunk)
        value = import_release(paths['bridges'], paths['redirects'], wanted,
                               release=release, source=base, checksums=checksums)
    install(root, value)
    return value

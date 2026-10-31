"""Versioned Main registry snapshots; workers merge local observations into them."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from storage.sharedFiles import read_json,write_json
from .registry import Registry

FORMAT='soccer-main-id-registry-v1'
CORE=('format','aliases','published','redirects','anchors','withheld')


def local_path(root):return Path(root)/'_system/identity/alex_ids.json'
def snapshot_path(root):return Path(root)/'_system/identity/main_registry.json'

def version(value):return hashlib.sha256(json.dumps({k:value.get(k,{}) for k in CORE},sort_keys=True,separators=(',',':')).encode()).hexdigest()


@lru_cache(maxsize=4)
def _snapshot(path,mtime,size):
    saved=read_json(Path(path));value={k:saved[k] for k in CORE if k in saved}
    return {'format':FORMAT,'authority':'main','version':version(value),'registry':value}


def snapshot(root):
    path=local_path(root)
    if not path.exists():return None
    stat=path.stat();return _snapshot(str(path),stat.st_mtime_ns,stat.st_size)


@lru_cache(maxsize=4)
def _installed_version(path,mtime,size):return read_json(Path(path)).get('version','')


def installed_version(root):
    path=snapshot_path(root)
    if not path.exists():return ''
    stat=path.stat();return _installed_version(str(path),stat.st_mtime_ns,stat.st_size)


def install(root,value):
    if value.get('format')!=FORMAT or value.get('authority')!='main' or version(value['registry'])!=value.get('version'):
        raise ValueError('Invalid Main identity registry snapshot')
    Registry(local_path(root),value=value['registry'])  # Structural/version validation before replacing the cache.
    write_json(snapshot_path(root),value)


def load(root,*,seeds=(),path=None):
    path=Path(path) if path is not None else local_path(root)
    authority=read_json(snapshot_path(root),missing=None)
    local=read_json(path,missing=None)
    if authority:
        if version(authority['registry'])!=authority['version']:raise ValueError('Main registry snapshot checksum mismatch')
        registry=Registry(path,value=authority['registry'])
        if local and local!=authority['registry']:registry.merge(local)
    else:
        first=next((Path(s) for s in seeds if Path(s).is_file()),None)
        registry=Registry(path,seed=first)
    for seed in seeds:
        seed=Path(seed)
        if seed.is_file() and seed.resolve()!=path.resolve():
            value=read_json(seed)
            if version(value)!=version(registry.value):registry.merge(value)
    return registry


def check_bundle(root,dataset):
    folder=Path(dataset)/'concordance'
    if not (folder/'bundle.json').is_file():return 'absent'
    from .build import table,invalidate_bundle
    manifest=read_json(folder/'bundle.json')
    from .build import FORMAT
    if manifest.get('format')!=FORMAT:
        from .build import invalidate_bundle
        invalidate_bundle(dataset,'Rebuild the unified fixture table with player-stat availability flags')
        return 'rebuild_required'
    registry=Registry(local_path(root))
    try:
        for relative,kind in (('full/fixtures.csv','match'),('full/teams.csv','team'),
                              ('scraped/teams.csv','team'),('scraped/players.csv','player')):
            registry.validate_rows(kind,table(folder/relative))
    except ValueError:
        invalidate_bundle(dataset,'Shared identity registry changed; rebuild the unified-fixture snapshot before R import')
        return 'rebuild_required'
    return 'aligned'

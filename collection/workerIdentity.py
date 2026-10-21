"""User-named worker identity and data-carried collection origins."""
from storage.layout import system_path
import json
from pathlib import Path
import re
import uuid

from storage.pandasStore import atomic_json

ORIGIN_FORMAT='soccer-analysis-origins-v1'


def valid_worker_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',value):
        raise ValueError('Worker ID must use 1-64 lowercase letters, numbers, underscores or hyphens')
    return value


def read_worker_id(root):
    root=Path(root)
    identity=system_path(root,'identity.json')
    if identity.exists():
        saved=json.loads(identity.read_text(encoding='utf-8'))
        if not isinstance(saved,dict):raise ValueError('data/identity.json must be an object with worker_id')
        return valid_worker_id(saved.get('worker_id'))
    legacy=system_path(root,'.ensemble-computer.json')
    if legacy.exists():return 'worker-'+str(json.loads(legacy.read_text(encoding='utf-8'))['id'])[:12]
    return 'legacy-unidentified'


def ensure_identity(root, worker_id=None):
    root=Path(root);path=system_path(root,'identity.json')
    identity=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(identity,dict):raise ValueError('data/identity.json must be an object with worker_id')
    name=worker_id or identity.get('worker_id')
    if name is None:
        legacy=read_worker_id(root)
        name=legacy if legacy!='legacy-unidentified' else 'worker-'+uuid.uuid4().hex[:12]
    identity={'worker_id':valid_worker_id(name),'instance_id':identity.get('instance_id') or uuid.uuid4().hex}
    if not path.exists() or json.loads(path.read_text(encoding='utf-8'))!=identity:atomic_json(path,identity)
    return identity


def read_origins(root):
    path=system_path(Path(root),'.ensemble-origins.json')
    if not path.exists():return {}
    saved=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(saved,dict) or saved.get('format')!=ORIGIN_FORMAT or not isinstance(saved.get('records'),dict):
        raise ValueError('Invalid data-carried origin registry')
    if any(not isinstance(value,list) or any(not isinstance(item,str) for item in value)
           for value in saved['records'].values()):raise ValueError('Invalid collection origin entries')
    return saved['records']


def save_origins(root, records):
    # Background imports and foreground match saves must not erase each other.
    from filelock import FileLock
    path=system_path(Path(root),'.ensemble-origins.json');path.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(path.with_suffix('.lock'))):
        merged=read_origins(root)
        for key,workers in records.items():merged[key]=sorted(set(merged.get(key,[]))|set(workers))
        atomic_json(path,{'format':ORIGIN_FORMAT,'records':merged})

"""Completed per-source indexing survives failed/aborted dataset publication."""
import hashlib
import json
from pathlib import Path

from storage.pandasStore import atomic_json
from storage.layout import full_path
from concordance import package_state as tracking

FORMAT='soccer-dataset-id-cache-v2'


def recipe():
    """Indexing rules change IDs; reporting/UI/update code does not."""
    import config
    project=config.PROJECT_ROOT
    paths=[project/'dataset_builder.py',project/'collection/datasetIndexCache.py']
    for directory in ('providers','models','storage','config'):
        paths.extend(p for p in (project/directory).rglob('*')
                     if p.is_file() and p.suffix in ('.py','.json','.yaml') and p.name!='workers.yaml')
    paths.extend(project/'collection'/name for name in (
        'selection.py','paths.py','jsonFiles.py','qualifiers.py','playerStatsSchema.py',
        'whoScoredData.py','legacyFotmobRows.py'))
    def content(path):
        if path==project/'config/settings.yaml':
            import yaml
            value=yaml.safe_load(path.read_text())
            if isinstance(value,dict) and isinstance(value.get('dataset'),dict):
                value['dataset'].pop('concordance_format',None)
            return tracking.digest(value)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return tracking.digest([(str(p.relative_to(project)),content(p)) for p in sorted(set(paths)) if p.is_file()])


def cache_path(root,source,signature):
    # The JSON writer adds a UUID suffix: a normal cache path may fit Windows'
    # limit while its atomic temporary path does not. Keep the extended prefix.
    origin=full_path(tracking.logical_root(Path(root).resolve()))
    key=tracking.digest(str(tracking.logical_root(Path(source).resolve()).resolve()))
    return origin/'_system/cache/dataset-ids'/key/(signature+'.json')


def valid(value,signature):
    if not isinstance(value,dict) or value.get('signature')!=signature:return False
    rows=value.get('rows')
    if not isinstance(rows,list) or len(rows)!=3:return False
    for group in rows:
        if not isinstance(group,list):return False
        for item in group:
            if (not isinstance(item,list) or len(item)!=2 or not isinstance(item[0],list)
                    or not isinstance(item[1],dict) or not isinstance(item[1].get('_ids'),dict)):
                return False
    if value.get('format')==FORMAT:
        return value.get('checksum')==tracking.digest([signature,rows])
    return 'format' not in value  # Migrate the old private cache only if it matches.


def load(path,signature):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8'))
        return value if valid(value,signature) else None
    except (OSError,ValueError,TypeError):return None


def save(path,value):
    signature=value['signature']
    if not valid(value,signature):raise ValueError('Incomplete source indexing cannot be checkpointed')
    path=Path(path)
    saved={**value,'format':FORMAT,'checksum':tracking.digest([signature,value['rows']])}
    atomic_json(path,saved)
    # Retain a few contexts for overlapping snapshots without unbounded growth.
    # Files are immutable by signature; active readers either finish or reindex.
    try:
        versions=sorted(path.parent.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for old in versions[3:]:
            if old!=path:old.unlink(missing_ok=True)
    except OSError:pass  # Pruning cannot invalidate a successfully saved receipt.

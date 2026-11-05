"""Completed per-source indexing survives failed/aborted dataset publication."""
import hashlib
import json
from pathlib import Path

from storage.pandasStore import atomic_json
from storage.layout import full_path
from concordance import package_state as tracking

FORMAT='soccer-dataset-id-cache-v2'


def recipe():
    """Fingerprint identity extraction, independently of statistics and publication."""
    import ast
    import config
    project=config.PROJECT_ROOT
    # Hash the projection functions, not the whole builder (which also packages
    # CSVs, resolves statistics and publishes the dataset).
    path=project/'dataset_builder.py'
    functions={'_identifier','_season_directory','_reviewed_provider_player_id','_id_row',
               '_provider_team_id','_cached_provider_fixtures','_checkpoint_id_rows','_source_id_rows'}
    projection=[]
    if path.is_file():
        projection=[ast.dump(node,include_attributes=False) for node in ast.parse(path.read_text()).body
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in functions]
    paths=[project/'providers'/name for name in ('identity.py','identityReview.py','teamProfiles.py',
           'teamScope.py','events.py','team_crosswalk.py')]
    paths.extend((project/'providers/concordance').glob('*.json'))
    paths.extend(project/'collection'/name for name in (
        'selection.py','qualifiers.py','whoScoredData.py','legacyFotmobRows.py'))
    return tracking.digest(['identity-projection-v3',projection,
        [(str(p.relative_to(project)),hashlib.sha256(p.read_bytes()).hexdigest())
         for p in sorted(set(paths)) if p.is_file()]])


def source_signature(source):
    """ID dependencies only: spatial maps/events and unrelated statistics don't invalidate IDs."""
    source=Path(source)
    context=tracking._snapshot.get()
    if context and str(source.resolve()) in context.get('id_sources',{}):
        return context['id_sources'][str(source.resolve())]
    paths=[source/name for name in ('config.json','schedule.json','identities.csv','player_match_stats.csv',
                                  'whoscored_schedule.json','whoscored_job.json')]
    markers=list((source/'match_checkpoints').glob('*/CURRENT.json'))
    if not markers:markers=[source/'dataset/CURRENT.json']
    for marker in markers:
        paths.append(marker)
        if marker.is_file():
            value=json.loads(marker.read_text(encoding='utf-8'));generation=value.get('generation','')
            if generation and Path(generation).name==generation:
                paths.extend(marker.parent/'snapshots'/generation/name for name in
                             ('matches.csv','teams.csv','players.csv','performances.csv'))
    for provider in ('fotmob','whoscored'):
        paths.extend((source/'raw'/provider).glob('*.json'))
    return tracking.digest([(p.relative_to(source).as_posix(),tracking.file_stamp(p)) for p in sorted(set(paths))])


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

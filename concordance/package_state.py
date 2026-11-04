"""Change tracking for saved-data packaging; no state is committed on failure."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

from storage.pandasStore import atomic_json

FORMAT = 'soccer-package-state-v1'
_snapshot = ContextVar('package_snapshot', default=None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def file_stamp(path):
    path = Path(path)
    if not path.is_file():
        return None
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def recipe():
    """Code and reviewed reference changes also invalidate cached projections."""
    import config
    project = config.PROJECT_ROOT
    paths = [project/'dataset_builder.py']
    for directory in ('collection', 'providers', 'fixtures', 'concordance', 'storage', 'config'):
        paths.extend(p for p in (project/directory).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.json', '.yaml'))
    def content(path):
        if path==project/'config/settings.yaml':
            import yaml
            value=yaml.safe_load(path.read_text())
            if isinstance(value,dict) and isinstance(value.get('dataset'),dict):
                value['dataset'].pop('concordance_format',None)
            return digest(value)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return digest([(str(p.relative_to(project)),content(p)) for p in sorted(set(paths))])


def source_signature(source):
    """Track raw additions/replacements, flat inputs, and active checkpoint generations."""
    source = Path(source)
    context = _snapshot.get()
    if context and str(source.resolve()) in context['sources']:
        return context['sources'][str(source.resolve())]
    paths = [source/name for name in ('config.json','job.json','schedule.json','identities.csv',
             'player_match_stats.csv','team_match_statistics.csv','fotmob_team_statistics.csv')]
    paths.extend((source/'raw').rglob('*.json'))
    markers = list((source/'match_checkpoints').glob('*/CURRENT.json'))
    markers.append(source/'dataset/CURRENT.json')
    for marker in markers:
        paths.append(marker)
        if marker.is_file():
            # Include active files too: edits to an existing generation must be detected.
            value = json.loads(marker.read_text(encoding='utf-8'))
            generation = value.get('generation', '')
            if generation and Path(generation).name == generation:
                paths.extend((marker.parent/'snapshots'/generation).glob('*.csv'))
    return digest([(p.relative_to(source).as_posix(), file_stamp(p)) for p in sorted(set(paths))])


def season_outputs(source, output, tables):
    import dataset_builder
    if not (Path(source)/'config.json').is_file():
        return {'unavailable':None}
    settings = json.loads((Path(source)/'config.json').read_text(encoding='utf-8'))
    directory = (Path(output)/'raw_data'/dataset_builder._slug(settings['competition'])/
                 dataset_builder._season_directory(settings['season']))
    return {table: file_stamp(directory/dataset_builder.SEASON_TABLES[table][1]) for table in tables}


def global_signature(root, compiled, sources, settings, recipe_id):
    root, compiled = Path(root), Path(compiled)
    context = _snapshot.get()
    paths = ([root/name for name in context['identity']] if context and root.resolve()==context['snapshot']
             else list((root/'_system/identity').glob('*.json')))
    paths.extend(compiled.glob('*.csv'))
    for directory in ('concordance','fixture_concordance'):
        paths.extend(p for p in (compiled/directory).rglob('*')
                     if p.is_file() and p.suffix in ('.csv','.json'))
    # Compact evidence delivered by workers can change fixture availability.
    paths.extend((logical_root(root).parent/'ensemble_data/workers').glob('*.json'))
    def entry(path):
        if path.is_relative_to(compiled):return ('compiled/'+path.relative_to(compiled).as_posix(),file_stamp(path))
        if context and path.is_relative_to(root):
            key=path.relative_to(root).as_posix()
            if key in context['identity']:return (str(logical_root(path)),context['identity'][key])
        return (str(logical_root(path)),file_stamp(path))
    return digest({'root':str(logical_root(root).resolve()), 'recipe':recipe_id, 'settings':settings,
                   'identity_context':identity_context(root),
                   'sources':[(str(p.relative_to(root)),source_signature(p)) for p in sources],
                   'inputs':sorted(entry(p) for p in set(paths))})


def publication_outputs(output):
    from .publication import TABLES, table_path, manifest_path, folder, publication_files
    base = folder(output)
    try:
        paths=publication_files(base)
    except (OSError,ValueError,TypeError,KeyError):
        # Track a missing/unreadable manifest as changed instead of an unchanged export.
        paths=[manifest_path(base), *(base/table_path(name) for name in TABLES)]
    return {p.relative_to(base).as_posix():file_stamp(p) for p in paths}


def load(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value,dict) or value.get('format') != FORMAT or not isinstance(value.get('seasons'),dict):
        return {}
    return value


def save(path, value):
    atomic_json(Path(path), {'format': FORMAT, **value}, indent=2)


def identity_context(root):
    from providers.identityReview import decisions
    from providers.reep import version
    reviews = sorted((row.get('action'),row.get('kind'),row.get('provider'),row.get('source_id'),
                      row.get('target_id')) for row in decisions(root))
    return digest([reviews,version(root)])


# Background builds use immutable, temporary snapshots. Keep their cache identities
# anchored to the original data root and freeze input stamps before export writes.
def logical_root(root):
    root = Path(root)
    context = _snapshot.get()
    if context and root.is_relative_to(context['snapshot']):
        return context['origin']/root.relative_to(context['snapshot'])
    return root


@contextmanager
def snapshot_inputs(origin, snapshot):
    from collection.paths import discover_collections
    origin, snapshot = Path(origin).resolve(), Path(snapshot).resolve()
    sources = {str(p.resolve()):source_signature(p) for p in discover_collections(snapshot,'config.json')}
    identity = {p.relative_to(snapshot).as_posix():file_stamp(p)
                for p in (snapshot/'_system/identity').glob('*.json')}
    token = _snapshot.set({'origin':origin,'snapshot':snapshot,'sources':sources,'identity':identity})
    try:
        yield
    finally:
        _snapshot.reset(token)

"""Load and validate the project's single user-editable YAML configuration."""
from pathlib import Path
import math

import yaml

CONFIG_FILE = Path(__file__).with_name('settings.yaml')
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """A configuration problem detected before any collection or build writes."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigError('YAML setting names must be strings')
        if key in result:
            raise ConfigError(f'Duplicate YAML setting: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _section(value, keys, label):
    if not isinstance(value, dict):
        raise ConfigError(f'{label} must be a YAML mapping')
    unknown = set(value) - set(keys)
    missing = set(keys) - set(value)
    if unknown:
        raise ConfigError(f'Unknown setting in {label}: {", ".join(sorted(unknown))}')
    if missing:
        raise ConfigError(f'Missing setting in {label}: {", ".join(sorted(missing))}')
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise ConfigError(f'{label} must be true or false (without quotes)')
    return value


def _strings(value, label, *, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty) or any(
            not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f'{label} must be a {"possibly empty" if allow_empty else "nonempty"} list of strings')
    return list(dict.fromkeys(value))


def load_config(path=None, *, project_root=None):
    """Read settings safely; resolve paths independently of the IDE working directory."""
    path = Path(CONFIG_FILE if path is None else path)
    root = Path(PROJECT_ROOT if project_root is None else project_root).resolve()
    try:
        document = yaml.load(path.read_text(encoding='utf-8'), Loader=_UniqueKeyLoader)
    except OSError as exc:
        raise ConfigError(f'Cannot read configuration {path}: {exc}') from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f'Invalid YAML in {path}: {exc}') from exc
    if isinstance(document,dict) and isinstance(document.get('collection'),dict):
        collection=document['collection']
        # Accept an existing settings file without forcing users to re-enter
        # their paths, stage IDs, profile settings or browser preferences.
        legacy=document.pop('transport',None)
        if legacy is not None:
            legacy=_section(legacy,('sofascore_mode','http','browser'),'legacy transport')
            for old,new in (('sofascore_mode','sofascore_access'),('http','http'),('browser','browser')):
                if new in collection:raise ConfigError(f'Both legacy transport and collection.{new} are set')
                collection[new]=legacy[old]
        legacy_who=document.pop('whoscored',None)
        if legacy_who is not None:
            if 'whoscored' in collection:raise ConfigError('Both legacy whoscored and collection.whoscored are set')
            collection['whoscored']=legacy_who
        collection.setdefault('types',['combined'])
        collection.setdefault('show_timing',True)
        collection.setdefault('sofascore_access','auto')
        collection.setdefault('http',{'profiles':['chrome','chrome131'],'languages':['en-US,en;q=0.9'],
            'proxy_env':'SOCCER_PROXY_URL','delay_min_seconds':8,'delay_max_seconds':15,'attempts':2})
        collection.setdefault('browser',{'channel':'chrome','delay_min_seconds':0.5,'delay_max_seconds':1,
            'timeout_seconds':30,'manual_wait_seconds':120})
        collection.setdefault('whoscored',{'request_delay_min_seconds':15,'request_delay_max_seconds':30,
            'refresh_schedule':False})
        document.setdefault('api_test', {'providers':['sofascore','whoscored','fotmob'],
            'match_ids':{'sofascore':12436870,'whoscored':1821095,'fotmob':4506485}})
    document = _section(document, ('paths', 'collection', 'dataset', 'api_test'), 'configuration')
    collection=_section(document['collection'],('action','types','competitions','seasons',
        'spatial_capture','build_dataset','show_timing','sofascore_access','http','browser','whoscored'),'collection')
    kinds=_strings(collection['types'],'collection.types')
    if set(kinds)-{'combined','sofascore','whoscored_events','fotmob_players'}:
        raise ConfigError('collection.types must contain combined, sofascore, whoscored_events, or fotmob_players')
    if 'combined' in kinds and 'sofascore' in kinds:
        raise ConfigError('Choose combined OR sofascore; they share the same match checkpoints')
    transport={'sofascore_mode':collection['sofascore_access'],'http':collection['http'],'browser':collection['browser']}
    if transport['sofascore_mode'] not in ('http','browser','auto','extension'):
        raise ConfigError('collection.sofascore_access must be http, browser, auto, or extension')
    http=_section(transport['http'],('profiles','languages','proxy_env','delay_min_seconds',
                                  'delay_max_seconds','attempts'),'collection.http')
    browser=_section(transport['browser'],('channel','delay_min_seconds','delay_max_seconds',
                                        'timeout_seconds','manual_wait_seconds'),'collection.browser')
    _strings(http['profiles'],'collection.http.profiles')
    _strings(http['languages'],'collection.http.languages')
    if not isinstance(http['proxy_env'],str) or (http['proxy_env'] and
            not http['proxy_env'].replace('_','a').isalnum()):
        raise ConfigError('collection.http.proxy_env must be an environment variable name or empty')
    if type(http['attempts']) is not int or not 1 <= http['attempts'] <= 3:
        raise ConfigError('collection.http.attempts must be 1 to 3')
    if browser['channel'] not in ('chrome','msedge','chromium'):
        raise ConfigError('collection.browser.channel must be chrome, msedge, or chromium')
    for label,section in (('http',http),('browser',browser)):
        lo,hi=section['delay_min_seconds'],section['delay_max_seconds']
        if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in (lo,hi)) or lo>hi:
            raise ConfigError(f'collection.{label} delays must be finite and 0 <= min <= max')
    for key in ('timeout_seconds','manual_wait_seconds'):
        v=browser[key]
        if type(v) not in (int,float) or not math.isfinite(v) or v <= 0:
            raise ConfigError(f'collection.browser.{key} must be positive and finite')
    api_test = _section(document['api_test'], ('providers','match_ids'), 'api_test')
    providers = _strings(api_test['providers'], 'api_test.providers')
    supported = ('sofascore','whoscored','fotmob')
    if set(providers) - set(supported):
        raise ConfigError('api_test.providers must contain only sofascore, whoscored, or fotmob')
    match_ids = _section(api_test['match_ids'],supported,'api_test.match_ids')
    if any(type(value) is not int or value < 1 for value in match_ids.values()):
        raise ConfigError('api_test.match_ids must be positive integer provider IDs')
    if isinstance(collection['whoscored'],dict):
        collection['whoscored'].setdefault('stage_ids',{})
        collection['whoscored'].setdefault('access','http')
    whoscored = _section(collection['whoscored'], (
        'request_delay_min_seconds','request_delay_max_seconds','refresh_schedule','stage_ids','access'), 'whoscored')
    if whoscored['access'] not in ('http','auto','browser','extension'):
        raise ConfigError('whoscored.access must be http, auto, browser, or extension')
    stage_ids=whoscored['stage_ids']
    if (not isinstance(stage_ids,dict) or any(
            not isinstance(key,str) or not isinstance(ids,list) or not ids
            or any(type(value) is not int or value < 1 for value in ids)
            for key,ids in stage_ids.items())):
        raise ConfigError('whoscored.stage_ids must map competition season labels to nonempty lists of positive IDs')
    minimum,maximum = (whoscored[key] for key in (
        'request_delay_min_seconds','request_delay_max_seconds'))
    if (any(type(value) not in (int,float) or not math.isfinite(value) or value <= 0
            for value in (minimum,maximum)) or minimum > maximum):
        raise ConfigError('whoscored request delays must be finite positive numbers with min <= max')
    paths = _section(document['paths'], ('data', 'dataset'), 'paths')
    dataset = _section(document['dataset'], (
        'collections', 'tables', 'references', 'maintenance'), 'dataset')
    tables = _section(dataset['tables'], (
        'events', 'player_match_stats', 'team_match_stats', 'fixtures'), 'dataset.tables')
    references = _section(dataset['references'], (
        'player_ids', 'team_ids', 'match_ids'), 'dataset.references')
    maintenance = _section(dataset['maintenance'], (
        'refresh_published_team_ids', 'standardize_fixture_team_names'), 'dataset.maintenance')
    resolved = {}
    for name, value in paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'paths.{name} must be a nonempty path string')
        target = Path(value).expanduser()
        resolved[name] = (target if target.is_absolute() else root / target).resolve()
    data, output = resolved['data'], resolved['dataset']
    if data == output or data in output.parents or output in data.parents:
        raise ConfigError('Data and dataset roots must be separate directories')
    if collection['action'] not in ('auto', 'start', 'resume', 'export', 'status', 'probe'):
        raise ConfigError('collection.action must be auto, start, resume, export, status, or probe')
    if collection['spatial_capture'] not in ('fast', 'all'):
        raise ConfigError('collection.spatial_capture must be fast or all')
    values = {
        'TRANSPORT':transport,
        'COLLECTION_TYPES':kinds,
        'SHOW_TIMING':_boolean(collection['show_timing'],'collection.show_timing'),
        'DATA_ROOT': data, 'DATASET_ROOT': output,
        'ACTION': collection['action'],
        'COMPETITIONS': _strings(collection['competitions'], 'collection.competitions'),
        'SEASONS': _strings(collection['seasons'], 'collection.seasons'),
        'SOFASCORE_SPATIAL_CAPTURE': collection['spatial_capture'],
        'BUILD_DATASET': _boolean(collection['build_dataset'], 'collection.build_dataset'),
        'COLLECTION_NAMES': tuple(_strings(dataset['collections'], 'dataset.collections', allow_empty=True)),
        'WHOSCORED_STAGE_IDS':stage_ids,
        'WHOSCORED_ACCESS':whoscored['access'],
        'WHOSCORED_MIN_DELAY':minimum,
        'API_TEST_PROVIDERS':providers,
        'API_TEST_MATCH_IDS':dict(match_ids),
        'WHOSCORED_MAX_DELAY':maximum,
        'WHOSCORED_REFRESH_SCHEDULE':_boolean(whoscored['refresh_schedule'], 'whoscored.refresh_schedule'),
    }
    for section_name, section in (('tables', tables), ('references', references)):
        for name, value in section.items():
            values['BUILD_' + name.upper()] = _boolean(value, f'dataset.{section_name}.{name}')
    for name, value in maintenance.items():
        values[name.upper()] = _boolean(value, f'dataset.maintenance.{name}')
    if values['STANDARDIZE_FIXTURE_TEAM_NAMES'] and not values['BUILD_TEAM_IDS']:
        raise ConfigError('dataset.maintenance.standardize_fixture_team_names requires dataset.references.team_ids')
    return values


# Internal defaults consumed by both entry points, not another settings file.
globals().update(load_config())

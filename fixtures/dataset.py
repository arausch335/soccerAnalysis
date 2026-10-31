"""Sofascore-led fixture history and four-source identity concordance.

Run ``python -m fixtures`` or ``python main.py --fixtures``. This only reads
schedules: no match details, lineups, events or player statistics are requested.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from filelock import FileLock
import pandas as pd

import config
from collection.files import atomic_csv
from collection.selection import (COMPETITIONS as COLLECTION_COMPETITIONS,
                                  months_between,
                                  resolve_sofascore_season, season_key)
from dataset_builder import FIXTURE_COLUMNS, _cached_provider_fixtures, fixtures_from_events
from providers.events import whoscored_fixture
from providers.scores import final_score
from providers.httpClient import ProviderError
from providers.identity import teams_match
from providers.teamProfiles import canonical_team_id
from providers.reep import PROVIDERS, index
from providers.whoScoredClient import _Directory, directory_path
from storage.layout import system_path
from storage.pandasStore import atomic_json
from .reconcile import reconcile, summarize, ATTEMPT_COLUMNS
from .sharedIds import assign_ids,FIXTURE_KEYS,REFERENCE_KEYS
from .builder import (COMPETITIONS, EUROPEAN_COMPETITIONS, _fotmob_event,
                      _slug, current_season_start, season_label)


# IDE defaults. Years mean season start years: 2010 = 2010/2011.
START_SEASON = 2010
END_SEASON = None
INCLUDE_UPCOMING = True
REFRESH_CURRENT = False  # Opt in to refresh; historical checkpoints are reused.
REFRESH_SCHEDULES = False
OFFLINE = False
REFRESH_REEP = False    # Optional public bulk refresh after discovering new IDs.
SEND_TO_MAIN = True    # Compiled files use Main reporting, never MSI raw storage.

SOURCES = (*PROVIDERS, 'reep')
ID_COLUMNS = tuple(f'{p}_{entity}_id' for entity in ('match', 'home_team', 'away_team')
                   for p in SOURCES)
NAME_COLUMNS = tuple(f'{p}_{side}_team' for p in PROVIDERS for side in ('home', 'away'))
CHECK_COLUMNS = tuple(f'{p}_{field}' for p in PROVIDERS[1:]
                      for field in ('fixture_check', 'match_method'))
COLUMNS = (*FIXTURE_COLUMNS, *ID_COLUMNS, *NAME_COLUMNS, *CHECK_COLUMNS,
           'whoscored_stage_id','whoscored_stage','whoscored_round',
           'match_identity_status', 'home_team_identity_status', 'away_team_identity_status',*FIXTURE_KEYS)
ERRORS = (ProviderError, OSError, ValueError, KeyError, TypeError)
FORMAT = 'soccer-fixture-schedule-v2'
SCHEDULE_SCOPE = 'all-published-stages-and-supplements-v2'


class _PartialSchedule(ProviderError):
    def __init__(self, events, cause):
        super().__init__(str(cause))
        self.events = list(events)
        self.status_code = getattr(cause, 'status_code', None)


def _validate(events,provider=''):
    seen = set()
    for event in events:
        if (type(event.get('id')) is not int or type(event.get('startTimestamp')) is not int
                or event['id'] in seen):
            raise ValueError('Invalid or duplicate schedule match ID/timestamp')
        seen.add(event['id'])
        for side in ('home', 'away'):
            team = event.get(side + 'Team') or {}
            if team.get('id') is None or not str(team.get('name') or '').strip():
                raise ValueError('Schedule fixture is missing a team ID/name')
            from providers.teamScope import in_mens_scope
            if not in_mens_scope(provider,team['id'],team):
                raise ValueError('Women\'s team returned in a men\'s fixture schedule')
    return events


def _checkpoint(root, provider, competition, start):
    return system_path(root, 'fixture_schedules') / 'cross_provider' / provider / _slug(competition.name) / (season_label(start) + '.json')


def _validate_sofa(events, competition, season_id=None):
    _validate(events,'sofascore')
    for event in events:
        cid = ((event.get('tournament') or {}).get('uniqueTournament') or {}).get('id')
        sid = (event.get('season') or {}).get('id')
        if cid not in (None, competition.sofascore_id) or season_id is not None and sid not in (None, season_id):
            raise ValueError('Sofascore returned another competition/season')
    return events


def _load(path, provider, competition, start):
    value = json.loads(path.read_text(encoding='utf-8'))
    if ((value.get('provider'), value.get('competition_id'), value.get('season')) != (
            provider, competition.sofascore_id, season_label(start))
            or value.get('format') not in (FORMAT, 'soccer-fixture-schedule-v1')):
        raise ValueError('Schedule checkpoint identity mismatch: ' + str(path))
    return (_validate_sofa(value['events'], competition) if provider == 'sofascore'
            else _validate(value['events'],provider))


def _complete_checkpoint(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    return value.get('format') == FORMAT and value.get('scope') == SCHEDULE_SCOPE


def _saved_source(root, competition, start):
    return Path(root) / _slug(competition.name) / season_label(start)


def _who_paths(client, competition):
    existing = next((c for c in COLLECTION_COMPETITIONS if c.name == competition.name), None)
    if existing:
        return [existing.whoscored_path, *existing.whoscored_supplemental_paths]
    # Read published navigation links; never invent tournament or stage IDs.
    parent = {'Championship':'Premier League', 'La Liga 2':'La Liga',
              '2. Bundesliga':'Bundesliga', 'Serie B':'Serie A',
              'Ligue 2':'Ligue 1', 'Conference League':'Champions League'}[competition.name]
    suffixes = {
        'Championship': ('england-championship',),
        'La Liga 2': ('spain-segunda-division', 'spain-segunda-divisi%C3%B3n', 'spain-laliga2', 'spain-laliga-2'),
        '2. Bundesliga': ('germany-bundesliga-ii', 'germany-2-bundesliga'),
        'Serie B': ('italy-serie-b',), 'Ligue 2': ('france-ligue-2',),
        'Conference League': ('europe-conference-league', 'europe-europa-conference-league'),
    }[competition.name]
    def discovered(parser):
        paths = set()
        for raw in (*parser.competition_paths, *parser.links):
            try: path = directory_path(raw)
            except (ValueError, UnicodeError): continue
            if any(re.search(re.escape(suffix)+r'(?:-\d{4}-\d{4})?/?$',path,re.I) for suffix in suffixes):
                paths.add(path)
        return paths
    parent_path=next(c.whoscored_path for c in COLLECTION_COMPETITIONS if c.name==parent)
    if callable(getattr(client,'_directory',None)):
        parser=client._directory(parent_path,lambda p:bool(discovered(p)))
    else:
        parser=_Directory();parser.feed(client.get_text(parent_path))
    paths=discovered(parser)
    # A tournament dropdown gives the stable competition route. Prefer it to
    # links to individual stages of its currently selected season.
    stable={p for p in paths if '/seasons/' not in p.lower()}
    if stable:paths=stable
    if len(paths) != 1:
        raise ValueError('WhoScored competition directory is unavailable or ambiguous: ' + competition.name)
    return sorted(paths)


def _who_events(client, competition, start, baseline, *, cache_root=None, refresh=False):
    season = f'{start}/{start + 1}'
    events, errors = {}, []
    client.fixture_scope = {'competition':competition.name,'season':season,'stages':[]}
    # Include June qualifiers even when the baseline only has main-stage games;
    # COVID-delayed seasons can extend into the following summer.
    months = sorted(set(months_between(baseline)) | {
        f'{year}{month:02d}' for year, values in ((start, range(6,13)), (start+1, range(1,7))) for month in values})

    def failed(exc):
        if getattr(exc, 'status_code', None) in (401,403,429):
            raise exc
        errors.append(str(exc))

    def collect_path(path, qualifying=False):
        prepare=getattr(client,'register_fixture_season',None)
        if callable(prepare):prepare(path,season)
        found = [s for s in client.seasons(path) if season_key(s['name']) == season]
        if len(found) != 1:
            raise ValueError(f'WhoScored season {competition.name} {season} unavailable or ambiguous')
        # Configured IDs are hints for other collection workflows. A full
        # fixture history always enumerates the season's published stages.
        stages = list(dict.fromkeys(client.season_stages(found[0]['path'])))
        for stage in stages:
            names=getattr(client,'stage_names',{})
            name=names.get(stage,'') if isinstance(names,dict) else ''
            client.fixture_scope['stages'].append(dict(id=stage,name=name,competition_path=path,
                season_path=found[0]['path'],qualifying=qualifying,months=months))
            for month in months:
                try:
                    cache=(Path(cache_root)/'_system/fixtures/whoscored_months'/_slug(competition.name)/season_label(start)/str(stage)/(month+'.json')) if cache_root is not None else None
                    expected={'format':'soccer-whoscored-month-v1','season_path':found[0]['path'],
                              'stage':stage,'month':month}
                    saved=json.loads(cache.read_text(encoding='utf-8')) if cache is not None and cache.is_file() and not refresh else None
                    if saved is not None:
                        if any(saved.get(k)!=v for k,v in expected.items()):raise ValueError('WhoScored month checkpoint identity differs')
                        monthly=saved['matches']
                    else:monthly=client.season_events(stage, month)
                    for row in monthly:
                        event = whoscored_fixture(row)
                        event['stageInfo'] = {'id':stage, 'name':name}
                        event['tournament'] = {'qualificationOrPreliminary':qualifying,
                            'name':event['stageInfo']['name'] or competition.name}
                        if competition.name in ('Bundesliga','2. Bundesliga','Ligue 1','Ligue 2') and 'qualification' in path.lower():
                            event['tournament']['name'] = competition.name + ', Promotion/Relegation Playoffs'
                        event['roundInfo'] = {'round':row.get('round'), 'name':row.get('roundName')}
                        prior = events.get(event['id'])
                        if prior is not None and any(prior.get(k)!=event.get(k) for k in
                                ('startTimestamp','homeTeam','awayTeam','homeScore','awayScore','status')):
                            raise ValueError('Contradictory WhoScored schedule entries')
                        events[event['id']] = event
                    if cache is not None and saved is None:atomic_json(cache,{**expected,'matches':monthly})
                except ERRORS as exc:
                    failed(ProviderError(f'WhoScored stage {stage} month {month}: {exc}')
                           if getattr(exc,'status_code',None) not in (401,403,429) else exc)

    try:
        paths = _who_paths(client, competition)
        for path in dict.fromkeys(paths):
            try:collect_path(path)
            except ERRORS as exc:failed(exc)
        domestic = {
            '2. Bundesliga': (673,2010),
            'Ligue 1': (660,2016),
            'Ligue 2': (661,2016),
        }.get(competition.name)
        if domestic and start >= domestic[1]:
            try:collect_path(client.supplemental_path(paths[0], domestic[0]))
            except ERRORS as exc:failed(exc)
        if competition.name in EUROPEAN_COMPETITIONS:
            try:
                qualifying = client.qualification_path(paths[0])
                if qualifying in paths:raise ValueError('WhoScored qualifying directory resolved to the main competition')
                collect_path(qualifying, True)
            except ERRORS as exc:failed(exc)
    except ERRORS as exc:
        if events:raise _PartialSchedule(events.values(), exc) from exc
        raise
    if errors:raise _PartialSchedule(events.values(), ValueError('; '.join(errors)))
    return list(events.values())


def _fotmob_events(client, competition, start):
    """Keep the primary schedule if a supplemental feed lacks this season."""
    season = f'{start}/{start + 1}'
    feeds = [(competition.fotmob_id, False), (competition.fotmob_qualification_id, True),
             (competition.fotmob_playoff_id, False)]
    events, errors = {}, []
    for cid, qualifying in feeds:
        if cid is None:
            continue
        try:
            if season not in client.seasons(cid):
                raise ValueError(f'No season {season} for FotMob competition {cid}')
            for row in client.season_events(cid, season):
                event = _fotmob_event(row, cid, start, qualifying)
                if cid == competition.fotmob_playoff_id:
                    event['tournament']['name'] = 'Bundesliga, Relegation/Promotion Playoffs'
                previous = events.get(event['id'])
                # The primary and qualifying feeds can overlap in some years.
                if previous:
                    if any(previous[key] != event[key] for key in ('startTimestamp', 'homeTeam', 'awayTeam')):
                        raise ValueError('Conflicting FotMob fixture IDs across feeds')
                    if all(e.get('status',{}).get('type')=='finished' for e in (previous,event)) and any(final_score(previous,side)!=final_score(event,side) for side in ('home','away')):
                        raise ValueError('Conflicting FotMob results across feeds')
                    if qualifying:previous['tournament']['qualificationOrPreliminary']=True
                    continue
                events[event['id']] = event
        except ERRORS as exc:
            if cid == competition.fotmob_id or getattr(exc, 'status_code', None) in (401, 403, 429):
                if events:
                    raise _PartialSchedule(events.values(), exc) from exc
                raise
            errors.append(f'FotMob feed {cid}: {exc}')
    return list(events.values()), errors


def schedules(root, clients, competition, start, *, offline=False, refresh=False,
              blocked=None):
    """Checkpoints are full schedules, independent of output filtering switches."""
    result, coverage = {}, []
    blocked = {} if blocked is None else blocked
    source = _saved_source(root, competition, start)
    for provider in PROVIDERS:
        path = _checkpoint(root, provider, competition, start)
        origin = 'checkpoint'
        scope_details = {}
        error = ''
        events = []
        complete = True
        try:
            reusable = path.exists() and _complete_checkpoint(path)
            if path.exists():
                events = _load(path, provider, competition, start)
                scope_details = json.loads(path.read_text(encoding='utf-8')).get('scope_details',{})
            if reusable and not refresh:
                pass
            else:
                # Collection schedules predate full-scope fixture checkpoints.
                # Retain them as a fallback, but never claim qualifiers/playoffs
                # were fetched just because the main schedule exists.
                if provider == 'sofascore' and not events and (source / 'schedule.json').exists():
                    cfg = source / 'config.json'
                    saved = json.loads(cfg.read_text(encoding='utf-8')) if cfg.exists() else {}
                    if saved.get('id_namespace', saved.get('baseline_provider', 'sofascore')) != 'sofascore':
                        raise ValueError('Saved schedule is not in the Sofascore ID namespace')
                    if saved.get('competition', competition.name) != competition.name or season_key(saved.get('season', f'{start}/{start+1}')) != f'{start}/{start+1}':
                        raise ValueError('Saved collection belongs to another competition/season')
                    events = _validate_sofa(json.loads((source / 'schedule.json').read_text(encoding='utf-8'))['events'], competition)
                    origin = 'saved_collection'
                if provider != 'sofascore' and not events and not refresh:
                    events = _cached_provider_fixtures(source, provider)
                if offline:
                    if not events:
                        raise ValueError('No saved fixture schedule (offline)')
                    complete = False  # Local match caches need not cover a whole season.
                    origin = 'legacy_checkpoint' if path.exists() else origin if provider == 'sofascore' else 'saved_provider_cache'
                    error = 'Saved schedule has no proof of full stage/qualifying scope; online upgrade required'
                else:
                    if provider in blocked:
                        raise ValueError('Provider paused for this run: ' + blocked[provider])
                    client = clients[provider]
                    client.refresh_cache = refresh
                    try:
                        if provider == 'sofascore':
                            season = resolve_sofascore_season(client, competition, season_label(start))
                            events = list(events)
                            fetched = {}
                            for event in client.iter_season_events(competition.sofascore_id, season['id'], include_upcoming=True):
                                _validate_sofa([event], competition, season['id'])
                                if event['id'] in fetched and fetched[event['id']] != event:
                                    raise ValueError('Conflicting Sofascore schedule entries')
                                fetched[event['id']] = event
                                events = list({e['id']:e for e in (*events,event)}.values())
                            events = list(fetched.values())
                        elif provider == 'fotmob':
                            events, feed_errors = _fotmob_events(client, competition, start)
                            if feed_errors:
                                complete = False
                                error = '; '.join(feed_errors)
                        else:
                            events = _who_events(client, competition, start, result['sofascore'],cache_root=root,refresh=refresh)
                            scope_details = client.fixture_scope
                        origin = 'provider_schedule'
                    finally:
                        client.refresh_cache = False
            _validate(events,provider)
            if not events:
                raise ValueError('No fixtures returned')
            if complete and competition.name in EUROPEAN_COMPETITIONS:
                phases = {bool((e.get('tournament') or {}).get('qualificationOrPreliminary')) for e in events}
                if phases != {False,True}:
                    raise _PartialSchedule(events,ValueError('Main and qualifying fixtures are not both present'))
            if complete and origin != 'checkpoint':
                atomic_json(path, {'format': FORMAT, 'provider': provider,
                    'competition_id': competition.sofascore_id, 'season': season_label(start),
                    'scope': SCHEDULE_SCOPE, 'scope_details':scope_details, 'events': events})
        except ERRORS as exc:
            if provider=='whoscored':
                detail=getattr(clients.get(provider),'fixture_scope',{})
                if isinstance(detail,dict):scope_details=detail
            error = f'{type(exc).__name__}: {exc}'
            complete = False
            if (getattr(exc, 'status_code', None) in (401, 403, 429)
                    or getattr(clients.get(provider), 'requires_manual_attention', False) is True):
                blocked[provider] = error
            # A failed refresh must retain the last complete schedule.
            if path.exists():
                try:
                    prior = _load(path, provider, competition, start)
                    events = list({e['id']:e for e in (*prior,*events)}.values())
                except ERRORS as invalid:error += '; unusable checkpoint: ' + str(invalid)
                origin = 'stale_checkpoint'
            elif provider != 'sofascore':
                events = _cached_provider_fixtures(source, provider)
                origin = 'saved_provider_cache'
            if isinstance(exc, _PartialSchedule):
                retained = {e['id']: e for e in events}
                retained.update({e['id']: e for e in _validate(exc.events,provider)})
                events = list(retained.values())
                origin = 'partial_provider_schedule'
            if provider == 'sofascore' and not events:
                coverage.append(dict(competition=competition.name, season=season_label(start),
                    provider=provider, status='missing', fixtures=0, source=origin, error=error))
                coverage.extend(dict(competition=competition.name, season=season_label(start),
                    provider=p, status='not_attempted', fixtures=0, source='', error='Baseline schedule unavailable') for p in PROVIDERS[1:])
                return {}, coverage
        result[provider] = events
        coverage.append(dict(competition=competition.name, season=season_label(start), provider=provider,
            status='complete' if complete else 'partial' if events else 'missing',
            fixtures=len(events), source=origin, error=error,scope_details=json.dumps(scope_details,sort_keys=True)))
    return result, coverage


def _ids(bridges, kind, native):
    return bridges.resolve(kind, {p + '_id': str(native.get(p) or '') for p in PROVIDERS})


def _compatible(event, other, provider, root, bridges, learned=None, reverse=None):
    if abs(event['startTimestamp'] - other['startTimestamp']) > 6 * 3600:
        return False
    if bridges.same('match', 'sofascore', event['id'], provider, other['id']) is False:
        return False
    for side in ('home', 'away'):
        a, b = event[side + 'Team'], other[side + 'Team']
        linked = bridges.same('team', 'sofascore', a['id'], provider, b['id'])
        if linked is False:
            return False
        if not teams_match(a, b, provider, root):
            if (learned is None or learned.get((provider, str(a['id'])), set()) != {str(b['id'])}
                    or reverse.get((provider, str(b['id'])), set()) != {str(a['id'])}):
                return False
    if all(e.get('status', {}).get('type') == 'finished' for e in (event, other)):
        for side in ('home', 'away'):
            a, b = (final_score(e,side) for e in (event, other))
            if a is not None and b is not None and a != b:
                return False
    return True


def _opponent_compatible(event, other, provider, root, bridges, learned, reverse):
    """A known opponent plus the same dated result can establish a new club ID.

    Callers constrain candidates to the same competition/season and require a
    unique match. A scoreline alone never establishes two unknown team links.
    """
    if (abs(event['startTimestamp'] - other['startTimestamp']) > 6 * 3600
            or bridges.same('match', 'sofascore', event['id'], provider, other['id']) is False
            or any(e.get('status', {}).get('type') != 'finished' for e in (event, other))):
        return False
    known = []
    for side in ('home', 'away'):
        score, other_score = (final_score(e,side) for e in (event, other))
        if score is None or other_score is None or score != other_score:
            return False
        a, b = event[side + 'Team'], other[side + 'Team']
        if bridges.same('team', 'sofascore', a['id'], provider, b['id']) is False:
            return False
        from providers.identity import REVIEWED_TEAM_IDS
        reviewed = REVIEWED_TEAM_IDS.get((provider, str(a['id'])))
        if reviewed is not None and str(b['id']) != str(reviewed):
            return False
        peers = {canonical_team_id(provider,n) for n in learned.get((provider, str(a['id'])), set())}
        inverse = reverse.get((provider, str(b['id'])), set())
        if peers and peers != {canonical_team_id(provider,b['id'])} or inverse and inverse != {str(a['id'])}:
            return False
        known.append(teams_match(a, b, provider, root) or peers == {canonical_team_id(provider,b['id'])} and inverse == {str(a['id'])})
    return any(known) or bridges.same('match', 'sofascore', event['id'], provider, other['id']) is True


def concord(snapshots, root, *, include_upcoming=True):
    """Learn only fixture-validated links; report global ID collisions explicitly."""
    bridges = index(root)
    learned, reverse = defaultdict(set), defaultdict(set)
    seasonal, seasonal_reverse = defaultdict(set), defaultdict(set)
    observations, issues, aliases = [], [], []
    methods = {}
    days_by_season = {}
    def near(by_day,event):
        day=event['startTimestamp']//86400
        return [other for adjacent in (day-1,day,day+1) for other in by_day[adjacent]
                if abs(event['startTimestamp']-other['startTimestamp'])<=6*3600]
    for competition, start, schedules_by_provider in snapshots:
        by_day = {p: defaultdict(list) for p in PROVIDERS[1:]}
        for p in by_day:
            for other in schedules_by_provider.get(p, []):
                by_day[p][other['startTimestamp'] // 86400].append(other)
        days_by_season[competition.name, start] = by_day
        for event in schedules_by_provider['sofascore']:
            matches = {}
            for p in PROVIDERS[1:]:
                candidates = near(by_day[p],event)
                found = [e for e in candidates if _compatible(event, e, p, root, bridges, learned, reverse)]
                method = 'teams_date_score'
                if not found:
                    found = [e for e in candidates if _opponent_compatible(event, e, p, root, bridges, learned, reverse)]
                    method = 'opponent_date_score'
                linked = [e for e in found if bridges.same('match', 'sofascore', event['id'], p, e['id']) is True]
                if linked:
                    found = linked
                if len(found) == 1:
                    other = found[0]
                    matches[p] = other
                    methods[p, str(event['id'])] = method
                    for side in ('home', 'away'):
                        a, b = event[side + 'Team'], other[side + 'Team']
                        seasonal[competition.name,start,p,str(a['id'])].add(str(b['id']))
                        seasonal_reverse[competition.name,start,p,str(b['id'])].add(str(a['id']))
                        learned[p, str(a['id'])].add(str(b['id']))
                        reverse[p, str(b['id'])].add(str(a['id']))
                        aliases.append(dict(sofascore_team_id=str(a['id']), sofascore_name=a['name'],
                            provider=p, provider_team_id=str(b['id']), provider_name=b['name'],
                            sofascore_match_id=str(event['id']), provider_match_id=str(other['id']),
                            competition=competition.name, season=season_label(start), method='reep_team_id' if bridges.same('team', 'sofascore', a['id'], p, b['id']) is True else method))
                elif len(found) > 1:
                    issues.append(dict(sofascore_match_id=str(event['id']), provider=p,
                        issue='ambiguous_fixture', candidate_ids='|'.join(str(e['id']) for e in found)))
                else:
                    bridged = _ids(bridges, 'match', {'sofascore': event['id']}).get(p + '_id')
                    rejected = [e for e in schedules_by_provider.get(p, []) if str(e['id']) == bridged]
                    if rejected:
                        issues.append(dict(sofascore_match_id=str(event['id']), provider=p,
                            issue='reep_fixture_failed_validation', candidate_ids=bridged))
            observations.append((competition, start, event, matches))

    # A club first identified in a later season can resolve earlier name variants.
    # No fuzzy-name score becomes an ID mapping; both sides/date/scores still pass.
    for competition, start, event, matches in observations:
        by_day = days_by_season[competition.name, start]
        for p in PROVIDERS[1:]:
            if p in matches:
                continue
            found = [e for e in near(by_day[p],event)
                     if _compatible(event, e, p, root, bridges, learned, reverse)]
            if len(found) == 1:
                other = matches[p] = found[0]
                methods[p, str(event['id'])] = 'learned_team_ids_and_fixture'
                issues = [i for i in issues if not (i['sofascore_match_id'] == str(event['id']) and i['provider'] == p)]
                for side in ('home', 'away'):
                    a, b = event[side + 'Team'], other[side + 'Team']
                    aliases.append(dict(sofascore_team_id=str(a['id']), sofascore_name=a['name'],
                        provider=p, provider_team_id=str(b['id']), provider_name=b['name'],
                        sofascore_match_id=str(event['id']), provider_match_id=str(other['id']),
                        competition=competition.name, season=season_label(start), method='learned_team_ids_and_fixture'))

    # A provider fixture cannot be assigned to two different baseline fixtures.
    assignments = defaultdict(set)
    for _, _, event, matches in observations:
        for p, other in list(matches.items()):
            if not _compatible(event, other, p, root, bridges, learned, reverse):
                del matches[p]
                issues.append(dict(sofascore_match_id=str(event['id']), provider=p,
                    issue='learned_team_mapping_conflict', candidate_ids=str(other['id'])))
                continue
            assignments[p, str(other['id'])].add(str(event['id']))
    problem_map = {(i['sofascore_match_id'], i['provider']): i['issue'] for i in issues}
    rows, team_rows, match_rows = [], {}, []
    for competition, start, event, matches in observations:
        if not include_upcoming and event.get('status', {}).get('type') != 'finished':
            continue
        row = fixtures_from_events([event], competition.name, season_label(start), include_qualifying=True).iloc[0].to_dict()
        if row['status'] not in ('finished', 'inprogress') and not row['awarded']:
            row.update(home_score=None, away_score=None, winner_code=None)
        native_matches = {'sofascore': event['id']}
        for p, other in list(matches.items()):
            if len(assignments[p, str(other['id'])]) > 1:
                issues.append(dict(sofascore_match_id=str(event['id']), provider=p,
                    issue='provider_fixture_reused', candidate_ids=str(other['id'])))
                problem_map[str(event['id']), p] = 'provider_fixture_reused'
                del matches[p]
            else:
                native_matches[p] = other['id']
        resolved_match = _ids(bridges, 'match', native_matches)
        for p in SOURCES:
            row[p + '_match_id'] = resolved_match.get(p + '_id', '')
        row['match_identity_status'] = resolved_match['identity_status']
        who = matches.get('whoscored',{})
        row['whoscored_stage_id'] = (who.get('stageInfo') or {}).get('id','')
        row['whoscored_stage'] = (who.get('stageInfo') or {}).get('name','')
        row['whoscored_round'] = (who.get('roundInfo') or {}).get('name') or (who.get('roundInfo') or {}).get('round','')
        for p in PROVIDERS[1:]:
            problem = problem_map.get((str(event['id']), p), '')
            if problem:
                row[p + '_match_id'] = ''
                row['reep_match_id'] = ''
                row['match_identity_status'] = 'conflict'
            row[p + '_fixture_check'] = 'validated' if p in matches else problem or ('bridge_only' if row[p + '_match_id'] else 'missing')
            row[p + '_match_method'] = ('reep_and_fixture' if p in matches and bridges.same('match', 'sofascore', event['id'], p, matches[p]['id']) is True
                else methods[p, str(event['id'])] if p in matches else 'reep_bridge' if row[p + '_match_id'] else '')
        match_rows.append(dict(competition=row['competition'], season=row['season'], date=row['date'],
            home_team_name=row['home_team'], away_team_name=row['away_team'],
            **{p + '_id': row[p + '_match_id'] for p in SOURCES}, identity_status=row['match_identity_status']))
        for side in ('home', 'away'):
            team = event[side + 'Team']
            native = {'sofascore': team['id']}
            conflicts = set()
            for p in PROVIDERS[1:]:
                peers = seasonal[competition.name,start,p,str(team['id'])]
                if len({canonical_team_id(p,n) for n in peers}) > 1:
                    conflicts.add(p)  # Competing profiles within this season remain ambiguous.
                elif p in matches:
                    native[p] = matches[p][side+'Team']['id']
                elif len(peers) == 1 and len(seasonal_reverse[competition.name,start,p,next(iter(peers))]) == 1:
                    native[p] = next(iter(peers))
                elif not peers:
                    global_peers = learned[p,str(team['id'])]
                    if len(global_peers)==1 and len(reverse[p,next(iter(global_peers))])==1:
                        native[p]=next(iter(global_peers))
            resolved = _ids(bridges, 'team', native)
            if conflicts:
                resolved['identity_status'] = 'conflict'
                resolved['reep_id'] = ''
                for p in conflicts:
                    resolved[p + '_id'] = ''
                issues.append(dict(sofascore_match_id=str(event['id']), provider='|'.join(sorted(conflicts)),
                    issue=side + '_team_id_collision', candidate_ids=''))
            for p in SOURCES:
                row[f'{p}_{side}_team_id'] = resolved.get(p + '_id', '')
            row[side + '_team_identity_status'] = resolved['identity_status']
            row['sofascore_' + side + '_team'] = team['name']
            for p in PROVIDERS[1:]:
                row[p + '_' + side + '_team'] = matches[p][side + 'Team']['name'] if p in matches else ''
            from providers.teamProfiles import canonical_record
            reference=canonical_record(dict(competition=competition.name,season=season_label(start),team_name=team['name'],
                **{p+'_id':resolved.get(p+'_id','') for p in SOURCES},identity_status=resolved['identity_status']),reference=True)
            key = (competition.name, start, reference['sofascore_id'], tuple(reference.get(p + '_id', '') for p in SOURCES))
            team_rows[key] = reference
        rows.append(row)
    return rows, list(team_rows.values()), match_rows, aliases, issues


def export(snapshots, coverage, root, output, *, include_upcoming=True, scope=None):
    canonical, attempts, counts = [], [], {}
    for competition, start, feeds in snapshots:
        cleaned = {}
        for provider, events in feeds.items():
            cleaned[provider], audit = reconcile(events, provider, competition.name, start, bridges=index(root))
            attempts.extend(audit)
            counts[competition.name,season_label(start),provider] = summarize(events,cleaned[provider],competition.name)
        canonical.append((competition,start,cleaned))
    coverage = [{**c, **counts.get((c['competition'],c['season'],c['provider']),{})} for c in coverage]
    # The Premier League has 20 clubs throughout this scope. Other leagues can
    # change size, include playoffs, or have legitimately shortened seasons.
    for c in coverage:
        expected = 380 if c['competition']=='Premier League' and int(c['season'][:4]) < (scope or {}).get('current_season',current_season_start()) else None
        c['expected_finished'] = expected
        c['expected_fixtures'] = expected
        c['count_check'] = ('pass' if c.get('finished_fixtures')==expected and c.get('canonical_fixtures')==expected else 'mismatch') if expected else 'not_applicable'
    rows, teams, matches, aliases, issues = concord(canonical, root, include_upcoming=include_upcoming)
    identity_summary=assign_ids(root,rows,matches,teams,folder=Path(output).parent/'fixture_concordance')
    frame = pd.DataFrame(rows, columns=COLUMNS)
    for column in ('match_id', 'matchweek', 'home_team_id', 'away_team_id',
                   'home_score', 'away_score', 'winner_code'):
        frame[column] = pd.to_numeric(frame[column], errors='raise').astype('Int64')
    # Matching completeness is distinct from transport and schedule completeness.
    for c in coverage:
        subset=frame[(frame['season']==c['season']) & frame['competition'].isin((c['competition'],c['competition']+' Qualification'))]
        c['matched_fixtures'] = (int(subset[c['provider']+'_fixture_check'].eq('validated').sum())
                                 if c['provider']!='sofascore' else len(subset))
        c['unmatched_fixtures'] = len(subset)-c['matched_fixtures']
    if frame.sofascore_match_id.duplicated().any():
        raise ValueError('Duplicate baseline match IDs across competition seasons')
    if rows:
        frame = frame.sort_values(['kickoff_utc', 'sofascore_match_id'], kind='stable')
    output = Path(output)
    folder = output.parent / 'fixture_concordance'
    folder.mkdir(parents=True, exist_ok=True)
    atomic_csv(frame, output)
    for name, records, columns in (
        ('attempts', attempts, ATTEMPT_COLUMNS),
        ('team_ids', teams, ('competition','season','team_name', *(p + '_id' for p in SOURCES), 'identity_status',*REFERENCE_KEYS)),
        ('match_ids', matches, ('competition', 'season', 'date', 'home_team_name', 'away_team_name', *(p + '_id' for p in SOURCES), 'identity_status',*REFERENCE_KEYS)),
        ('team_aliases', aliases, ('sofascore_team_id', 'sofascore_name', 'provider', 'provider_team_id', 'provider_name', 'sofascore_match_id', 'provider_match_id', 'competition','season','method')),
        ('issues', issues, ('sofascore_match_id', 'provider', 'issue', 'candidate_ids')),
        ('coverage', coverage, ('competition', 'season', 'provider', 'status', 'fixtures', 'source', 'error','scope_details','raw_fixtures','canonical_fixtures','finished_fixtures','superseded_fixtures','phases','expected_finished','expected_fixtures','count_check','matched_fixtures','unmatched_fixtures')),
    ):
        atomic_csv(pd.DataFrame(records, columns=columns).drop_duplicates(), folder / (name + '.csv'))
    missing_ids = {column: int(frame[column].fillna('').eq('').sum()) for column in ID_COLUMNS}
    manifest = dict(format='soccer-cross-provider-fixtures-v2', updated_at=datetime.now(timezone.utc).isoformat(),
        baseline='sofascore', scope=scope or {}, rows=len(frame), reep=index(root).metadata,**identity_summary,
        schedule_coverage_complete=bool(coverage) and all(c['status'] == 'complete' for c in coverage),
        count_checks_passed=all(c.get('count_check')!='mismatch' for c in coverage),
        fixture_attempts=len(attempts), raw_fixture_rows=sum(c.get('raw_fixtures',0) for c in coverage if c['provider']=='sofascore'),
        count_mismatches=[c for c in coverage if c.get('count_check')=='mismatch'],
        missing_ids=missing_ids, all_four_ids_complete=not any(missing_ids.values()),
        all_provider_fixtures_validated=all(frame[p + '_fixture_check'].eq('validated').all() for p in PROVIDERS[1:]),
        issues=len(issues), fixture_checks={p: frame[p + '_fixture_check'].value_counts().to_dict() for p in PROVIDERS[1:]})
    atomic_json(folder / 'manifest.json', manifest)
    from providers.teamHistory import export_history
    export_history(root,output.parent)
    return manifest


def build(*, data_root=None, output_csv=None, clients=None, competitions=COMPETITIONS,
          start_season=START_SEASON, end_season=END_SEASON, offline=OFFLINE,
          refresh_current=REFRESH_CURRENT, include_upcoming=INCLUDE_UPCOMING,
          refresh_reep=REFRESH_REEP, refresh_schedules=REFRESH_SCHEDULES,
          send_to_main=SEND_TO_MAIN, api_only=False, now=None, report=print):
    root = Path(config.DATA_ROOT if data_root is None else data_root)
    output = Path(config.DATASET_ROOT / 'fixtures.csv' if output_csv is None else output_csv)
    current = current_season_start(now)
    end = current if end_season is None else end_season
    if type(start_season) is not int or type(end) is not int or not 2010 <= start_season <= end <= current:
        raise ValueError('Select season start years from 2010 through the current season')
    if offline and (refresh_current or refresh_reep or refresh_schedules):
        raise ValueError('Offline mode cannot refresh provider schedules or Reep')
    root.mkdir(parents=True, exist_ok=True)
    scope = dict(start_season=start_season, end_season=end, current_season=current, include_upcoming=include_upcoming,
                 competitions=[c.name for c in competitions])
    snapshots, coverage, blocked = [], [], {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(FileLock(str(system_path(root, '.collector.lock')), timeout=0))
        stack.enter_context(FileLock(str(output) + '.lock', timeout=0))
        if clients is None:
            if not api_only:
                from providers.transports.bridge import extension_scope,acquire_bridge
                stack.enter_context(extension_scope())
                bridge,_=acquire_bridge(report=report,
                    connect_timeout=config.TRANSPORT['extension']['manual_wait_seconds'],
                    diagnostics_dir=root/'_system/extension_diagnostics/fixtures',
                    session_path=root/'_system/config/extension-session.json')
                if not offline:bridge.start()
            if api_only:
                from providers.safeClient import SafeSofaScoreClient, SafeFotMobClient, SafeWhoScoredClient
                factories = dict(zip(PROVIDERS, (SafeSofaScoreClient, SafeFotMobClient, SafeWhoScoredClient)))
            else:
                from providers.transports.sofascore import SofaScoreTransport
                from providers.transports.fotmob import FotMobTransport
                from providers.transports.whoscored import WhoScoredTransport
                factories = dict(zip(PROVIDERS, (SofaScoreTransport, FotMobTransport, WhoScoredTransport)))
            clients = {}
            for p, factory in factories.items():
                client = clients[p] = factory(root, offline=offline)
                stack.callback(client.close)
        for competition in competitions:
            for start in range(max(start_season, competition.first_season), end + 1):
                feeds, counts = schedules(root, clients, competition, start, offline=offline,
                    refresh=refresh_schedules or refresh_current and start == current, blocked=blocked)
                coverage.extend(counts)
                if feeds:
                    snapshots.append((competition, start, feeds))
                report(f'{competition.name} {season_label(start)}: ' + '; '.join(
                    f"{c['provider']} {c['fixtures']} ({c['status']})" + (f" — {c['error']}" if c['error'] else '') for c in counts), flush=True)
        if not snapshots:
            # Retain an existing dataset if no baseline schedules could be loaded.
            folder = output.parent / 'fixture_concordance'
            folder.mkdir(parents=True, exist_ok=True)
            atomic_csv(pd.DataFrame(coverage), folder / 'coverage.csv')
            raise ValueError('No Sofascore schedules available; existing fixture dataset was preserved. See fixture_concordance/coverage.csv')
        manifest = export(snapshots, coverage, root, output, include_upcoming=include_upcoming, scope=scope)
        if refresh_reep:
            from providers.reep import refresh
            refresh(root, output.parent)
            manifest = export(snapshots, coverage, root, output, include_upcoming=include_upcoming, scope=scope)
        if send_to_main:
            from collection.compiledDelivery import deliver
            manifest['delivery'] = deliver(root, output.parent, fixture_csv=output, attempt=not offline, report=report)
    report(f"Wrote {manifest['rows']} fixtures to {output}. Coverage complete: {manifest['schedule_coverage_complete']}; count checks passed: {manifest['count_checks_passed']}", flush=True)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--output', type=Path, default=config.DATASET_ROOT / 'fixtures.csv')
    parser.add_argument('--start-season', type=int, default=START_SEASON)
    parser.add_argument('--end-season', type=int, default=END_SEASON)
    parser.add_argument('--competition', action='append', choices=[c.name for c in COMPETITIONS])
    parser.add_argument('--offline', action='store_true', default=OFFLINE)
    parser.add_argument('--refresh-current', action='store_true', default=REFRESH_CURRENT)
    parser.add_argument('--refresh-schedules', action='store_true', default=REFRESH_SCHEDULES,
                        help='Refresh selected historical and current schedules')
    parser.add_argument('--refresh-reep', action='store_true', default=REFRESH_REEP)
    parser.add_argument('--include-upcoming', action=argparse.BooleanOptionalAction, default=INCLUDE_UPCOMING)
    parser.add_argument('--api-only', action='store_true', help='Disable Chrome extension fallback')
    parser.add_argument('--send-to-main', action=argparse.BooleanOptionalAction, default=SEND_TO_MAIN,
                        help='Queue compiled files for direct delivery to Main over reporting/Tailscale')
    args = parser.parse_args(argv)
    selected = tuple(c for c in COMPETITIONS if not args.competition or c.name in args.competition)
    try:
        manifest = build(data_root=args.data_root, output_csv=args.output, competitions=selected,
            start_season=args.start_season, end_season=args.end_season, offline=args.offline,
            refresh_current=args.refresh_current, refresh_reep=args.refresh_reep,
            refresh_schedules=args.refresh_schedules, include_upcoming=args.include_upcoming,
            send_to_main=args.send_to_main, api_only=args.api_only)
        return 0 if manifest['schedule_coverage_complete'] and manifest['count_checks_passed'] else 2
    except (ProviderError, OSError, ValueError) as exc:
        print(f'Fixture build needs attention: {exc}', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

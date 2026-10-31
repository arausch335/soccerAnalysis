"""Rebuild saved exports, package season datasets, and refresh combined ID tables."""
from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

import pandas as pd

import config

from collection.files import atomic_csv
from collection.paths import collection_directory, collection_name, discover_collections
from collection.selection import is_qualification_event, season_label, competition_phase
from collection.jsonFiles import read_json
from providers.events import fotmob_fixture, whoscored_fixture
from providers.identity import (IdentityError, REJECTED_PLAYER_ID_LINKS,
                                REVIEWED_PROVIDER_PLAYER_ID_ALIASES, REVIEWED_TEAM_IDS,
                                canonical_player_name, match_fixture, name_key)
from providers.team_crosswalk import VERIFIED_TEAM_ID_OVERRIDES
from storage.pandasStore import PandasStore

SEASON_TABLES = {
    'events': ('events.csv', 'events.csv'),
    'player_match_stats': ('player_match_stats.csv', 'player_match_stats.csv'),
    'team_match_stats': ('fotmob_team_statistics.csv', 'team_match_stats.csv'),
    'fixtures': ('schedule.json', 'fixtures.csv'),
}
PLAYER_ID_COLUMNS = ('competition', 'season', 'team_name', 'player_name',
                     'sofascore_id', 'fotmob_id', 'whoscored_id')
TEAM_ID_COLUMNS = ('team_name', 'sofascore_id', 'fotmob_id', 'whoscored_id')
SOURCE_TEAM_ID_COLUMNS = ('competition', 'season', *TEAM_ID_COLUMNS)
MATCH_ID_COLUMNS = ('competition', 'season', 'date', 'home_team_name', 'away_team_name',
                    'sofascore_id', 'fotmob_id', 'whoscored_id')
PLAYER_PROVIDER_ID_COLUMNS = ('sofascore_id', 'fotmob_id', 'whoscored_id')
FIXTURE_COLUMNS = (
    'match_id', 'date', 'kickoff_utc', 'season', 'competition', 'matchweek', 'round_name',
    'status', 'awarded', 'home_team_id', 'home_team', 'away_team_id', 'away_team',
    'home_score', 'away_score', 'winner_code', 'venue', 'city', 'competition_phase', 'competition_note')
TEAM_NAME_OVERRIDES = {'9788': 'Borussia Mönchengladbach'}
TEAM_CROSSWALK = Path(__file__).resolve().parent / 'providers' / 'team_id_crosswalk.json'


def _slug(value):
    return re.sub(r'[^a-z0-9]+', '-', str(value).casefold()).strip('-')


def _season_directory(value):
    found = re.fullmatch(r'(\d{4})/(\d{4})', str(value))
    if not found: raise ValueError(f'Unexpected canonical season {value!r}')
    return f'{found.group(1)}-{found.group(2)}'


def _identifier(value):
    if value is None or value is pd.NA: return ''
    if isinstance(value, float):
        if pd.isna(value): return ''
        if value.is_integer(): return str(int(value))
    text = str(value).strip()
    return '' if text.casefold() in ('', 'nan', 'none', '<na>') else text


def _chosen(values):
    """Choose the most frequently observed nonblank ID, breaking ties by recency."""
    values = [_identifier(value) for value in values]
    values = [value for value in values if value]
    if not values: return ''
    counts = Counter(values);
    last = {value: index for index, value in enumerate(values)}
    return max(counts, key=lambda value: (counts[value], last[value]))


def _reviewed_provider_player_id(provider, baseline_id, provider_id):
    provider_id = _identifier(provider_id);
    baseline_id = _identifier(baseline_id)
    if (provider, provider_id, baseline_id) in REJECTED_PLAYER_ID_LINKS: return ''
    return REVIEWED_PROVIDER_PLAYER_ID_ALIASES.get((provider, provider_id), provider_id)


def _id_row(rows, key, base):
    return rows.setdefault(key, {**base, '_ids': {name: [] for name in
                                                  ('sofascore_id', 'fotmob_id', 'whoscored_id')}})


def _backfill_unambiguous_ids(rows):
    """Share stable provider IDs across seasons only when the component is unique."""
    entries = list(rows.values());
    parents = list(range(len(entries)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]];
            index = parents[index]
        return index

    def union(left, right):
        left = find(left);
        right = find(right)
        if left != right: parents[right] = left

    owners = {}
    for index, row in enumerate(entries):
        for column, values in row['_ids'].items():
            for value in {_identifier(item) for item in values} - {''}:
                token = (column, value)
                if token in owners:
                    union(index, owners[token])
                else:
                    owners[token] = index
    components = {}
    for index in range(len(entries)): components.setdefault(find(index), []).append(index)
    for indexes in components.values():
        for column in ('sofascore_id', 'fotmob_id', 'whoscored_id'):
            values = {_identifier(value) for index in indexes
                      for value in entries[index]['_ids'][column]} - {''}
            if len(values) != 1: continue
            value = next(iter(values))
            for index in indexes: entries[index]['_ids'][column].append(value)


def _finalize_id_rows(rows, columns, sort_columns):
    result = []
    for row in rows.values():
        candidates={name:sorted({_identifier(v) for v in items}-{''}) for name,items in row.pop('_ids').items()}
        values={name:items[0] if len(items)==1 else '' for name,items in candidates.items()}
        result.append({**row,**values,'provider_id_candidates':json.dumps(candidates,sort_keys=True)})
    frame = pd.DataFrame(result, columns=[*columns,'provider_id_candidates'])
    if not frame.empty: frame = frame.sort_values(sort_columns, kind='stable').reset_index(drop=True)
    return frame


def _fixture_team_names(path):
    """Index fixture names by FotMob ID, never by potentially shared name."""
    names = {}
    with Path(path).open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        required = {'home_team_id', 'home_team', 'away_team_id', 'away_team'}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f'{path} is missing fixture team columns')
        for record in reader:
            for side in ('home', 'away'):
                provider_key='fotmob_'+side+'_team_id'
                identifier = _identifier(record.get(provider_key) if provider_key in record else record[side + '_team_id'])
                name = str(record[side + '_team']).strip()
                if provider_key in record and not identifier:continue
                if not identifier or not name:
                    raise ValueError(f'{path} contains a blank {side} team ID or name')
                names.setdefault(identifier, Counter())[name] += 1
    return {identifier: TEAM_NAME_OVERRIDES.get(identifier) or
                        max(variants, key=lambda name: (len(name), variants[name], name.casefold()))
            for identifier, variants in names.items()}


def _team_crosswalk(path=TEAM_CROSSWALK):
    if not path.exists(): return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    return data['teams']


def _team_reference(source_teams, fixtures_path, *, crosswalk_path=TEAM_CROSSWALK):
    """Make a FotMob-keyed reference, retaining unknown provider IDs as NA."""
    from providers.teamProfiles import canonical_frame,canonical_team_id,canonical_team_name
    source_teams=canonical_frame(source_teams,reference=True)
    names = _fixture_team_names(fixtures_path) if fixtures_path.exists() else {}
    names={canonical_team_id('fotmob',native):canonical_team_name('fotmob',native,name) for native,name in names.items()}
    crosswalk = _team_crosswalk(crosswalk_path)
    source = {}
    for row in source_teams.to_dict('records'):
        fotmob = _identifier(row['fotmob_id'])
        if not fotmob: continue
        entry = source.setdefault(fotmob, {'names': Counter(),
                                           'sofascore_id': set(), 'whoscored_id': set()})
        if row['team_name']: entry['names'][row['team_name']] += 1
        for column in ('sofascore_id', 'whoscored_id'):
            value = _identifier(row[column])
            if value: entry[column].add(value)
    records = [];
    problems = []
    for fotmob in sorted(set(names) | set(source),
                         key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)):
        entry = source.get(fotmob, {})
        observed = entry.get('names', Counter())
        candidate_names = Counter(observed)
        if fotmob in names: candidate_names[names[fotmob]] += 1
        team_name = (TEAM_NAME_OVERRIDES.get(fotmob) or
                     (max(candidate_names, key=lambda name: (len(name), candidate_names[name],
                                                             name.casefold()))
                      if candidate_names else ''))
        team_name=canonical_team_name('fotmob',fotmob,team_name)
        published = crosswalk.get(fotmob, {})
        record = {'team_name': team_name, 'fotmob_id': fotmob}
        for column in ('sofascore_id', 'whoscored_id'):
            values = set(entry.get(column, set()))
            verified = _identifier(published.get(column))
            if verified:verified=canonical_team_id(column.removesuffix('_id'),verified)
            if verified: values.add(verified)
            reviewed = _identifier(VERIFIED_TEAM_ID_OVERRIDES.get(fotmob, {}).get(column))
            if reviewed:reviewed=canonical_team_id(column.removesuffix('_id'),reviewed)
            if reviewed: values.add(reviewed)
            if len(values) > 1:
                problems.append(f'{team_name} ({fotmob}) has conflicting {column}: '
                                + ', '.join(sorted(values)))
            record[column] = next(iter(values)) if len(values) == 1 else pd.NA
        if not team_name or not fotmob:
            missing = [column for column in ('team_name', 'fotmob_id') if not record[column]]
            problems.append(f'{team_name or "unnamed team"} ({fotmob}) lacks '
                            + ', '.join(missing))
        records.append(record)
    if problems:
        preview = '\n- '.join(problems)
        raise ValueError('Team identity audit failed; team_ids.csv was not replaced. '
                         f'{len(problems)} issue(s) across {len(records)} FotMob IDs:\n- '
                         + preview)
    return pd.DataFrame(records, columns=TEAM_ID_COLUMNS)


def standardize_fixture_team_names(fixtures_path, team_ids_path):
    """Replace fixture display names using the FotMob-keyed reference."""
    fixtures_path = Path(fixtures_path);
    team_ids_path = Path(team_ids_path)
    teams = pd.read_csv(team_ids_path, dtype=str, keep_default_na=False)
    if not set(TEAM_ID_COLUMNS).issubset(teams.columns):
        raise ValueError(f'{team_ids_path} is missing team reference columns')
    if teams.fotmob_id.duplicated().any():
        raise ValueError(f'{team_ids_path} has duplicate FotMob IDs')
    if teams[['team_name', 'fotmob_id']].apply(
            lambda column: column.str.strip().str.casefold().isin(
                ('', 'na', 'nan', 'none', '<na>'))).any().any():
        raise ValueError(f'{team_ids_path} has blank team names or FotMob IDs')
    names = dict(zip(teams.fotmob_id, teams.team_name))
    fixtures = pd.read_csv(fixtures_path, dtype=str, keep_default_na=False)
    if not (set(FIXTURE_COLUMNS)-{'competition_phase','competition_note'}).issubset(fixtures.columns):
        raise ValueError(f'{fixtures_path} is missing fixture columns')
    for side in ('home', 'away'):
        missing = set(fixtures[side + '_team_id']) - names.keys()
        if missing:
            raise ValueError(f'{team_ids_path} lacks {len(missing)} fixture FotMob '
                             f'IDs, including {", ".join(sorted(missing)[:10])}')
        fixtures[side + '_team'] = fixtures[side + '_team_id'].map(names)
    atomic_csv(fixtures, fixtures_path)


def _validate_player_identities(frame):
    """Reject high-confidence name/ID contradictions before replacing the CSV.

    Fully disjoint triplets with the same name are allowed because football has
    many genuine namesakes.  Two differing triplets are contradictory when
    they share at least one provider ID: that shared ID says they are the same
    player while another provider ID says they are not.
    """
    if frame.empty: return
    complete = frame.loc[(frame[list(PLAYER_PROVIDER_ID_COLUMNS)] != '').all(axis=1)].copy()
    if complete.empty: return
    complete['_name_key'] = complete['player_name'].map(name_key)
    conflicts = []

    for ids, group in complete.groupby(list(PLAYER_PROVIDER_ID_COLUMNS), sort=True):
        names = sorted(set(group['player_name']))
        if group['_name_key'].nunique() > 1:
            conflicts.append(
                f'IDs {tuple(ids)} are assigned to different names: {", ".join(names)}')

    for normalized, group in complete.groupby('_name_key', sort=True):
        signatures = sorted(set(group[list(PLAYER_PROVIDER_ID_COLUMNS)]
                                .itertuples(index=False, name=None)))
        contradictory = []
        for index, left in enumerate(signatures):
            for right in signatures[index + 1:]:
                if any(a == b for a, b in zip(left, right)):
                    contradictory.extend((left, right))
        if contradictory:
            names = sorted(set(group['player_name']))
            distinct = sorted(set(contradictory))
            conflicts.append(
                f'name {", ".join(names)} has conflicting ID sets: '
                + '; '.join(str(ids) for ids in distinct))

    if conflicts:
        shown = conflicts[:20]
        preview = '\n- '.join(shown)
        remainder = len(conflicts) - len(shown)
        suffix = f'\n- ... and {remainder} more conflict(s)' if remainder else ''
        raise ValueError(
            'Player identity audit failed. Review the source identity mapping; '
            'player_ids.csv was not replaced:\n- ' + preview + suffix)


def _provider_team_id(source, metadata, provider, side):
    relative = (metadata.get('raw_response_files') or {}).get(provider)
    if not relative: return ''
    path = source / relative
    if not path.exists(): return ''
    saved = read_json(path)
    if saved.get('provider') != provider:
        raise ValueError(f'{path}: expected {provider} response')
    payload = saved.get('payload') or {}
    if provider == 'fotmob':
        return _identifier((payload.get('general') or {}).get(side + 'Team', {}).get('id'))
    if provider == 'whoscored':
        data = payload.get('matchCentreData') or {}
        return _identifier((data.get(side) or {}).get('teamId'))
    return ''


def _cached_provider_fixtures(source, provider):
    converter = {'fotmob': fotmob_fixture, 'whoscored': whoscored_fixture}[provider]
    fixtures = {};
    folder = Path(source) / 'raw' / provider
    for path in folder.glob('*.json') if folder.exists() else ():
        try:
            # Match-detail responses can be large. Their saved envelope puts
            # `key` before `payload`, so skip them without parsing every match.
            with path.open(encoding='utf-8') as stream:
                if not re.search(r'"key"\s*:\s*"fixtures/', stream.read(1024)):
                    continue
            saved = read_json(path)
            if saved.get('provider') != provider or not str(saved.get('key', '')).startswith('fixtures/'):
                continue
            for record in saved.get('payload', []):
                fixture = converter(record);
                fixtures[fixture['id']] = fixture
        except (ValueError, KeyError, TypeError):
            continue
    if provider=='whoscored':
        from collection.whoScoredData import standalone_fixtures
        for fixture in standalone_fixtures(source):fixtures.setdefault(fixture['id'],fixture)
    return list(fixtures.values())


def _checkpoint_id_rows(source, events, competition, season, baseline, player_rows, team_rows, match_rows):
    """Concordance stays complete when foreground CSV exports are deferred."""
    markers=sorted(source.glob('match_checkpoints/*/CURRENT.json'))
    if not markers:return False
    for marker in markers:
        dataset=PandasStore(marker.parent).from_csv()
        for match in dataset.matches.values():
            mid=_identifier(match.id)
            if mid not in events:continue
            match_row=match_rows.get((competition,season,baseline,mid))
            if match_row is not None:
                for provider,pid in match.metadata.get('provider_match_ids',{}).items():
                    if provider in ('sofascore','fotmob','whoscored'):match_row['_ids'][provider+'_id'].append(pid)
            for side in ('home','away'):
                team=getattr(match,side+'_team');tid=_identifier(team.id)
                row=_id_row(team_rows,(competition,season,tid),{
                    'competition':competition,'season':season,'team_name':team.name})
                row['_ids'][baseline+'_id'].append(tid)
                for provider in ('fotmob','whoscored'):
                    row['_ids'][provider+'_id'].append(_provider_team_id(source,match.metadata,provider,side))
                lineup=getattr(match,side+'_lineup')
                for appearance in lineup.players if lineup else ():
                    player=appearance.player;pid=_identifier(player.id)
                    row=_id_row(player_rows,(competition,season,team.name,pid),{
                        'competition':competition,'season':season,'team_name':team.name,
                        'player_name':canonical_player_name(player.name)})
                    row['_ids'][baseline+'_id'].append(pid)
                    for provider,entry in appearance.stats.raw.get('_providers',{}).items():
                        if provider not in ('sofascore','fotmob','whoscored'):continue
                        linked=_reviewed_provider_player_id(provider,pid,entry.get('id'))
                        if linked:row['_ids'][provider+'_id'].append(linked)
    return True


def _source_id_rows(source, player_rows, team_rows, match_rows, review_root=None):
    source = Path(source);
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    competition = config['competition'];
    season = _season_directory(config['season'])
    baseline = config.get('id_namespace', config.get('baseline_provider', 'sofascore'))
    if baseline not in ('sofascore', 'fotmob', 'whoscored'):
        raise ValueError(f'{source.name}: unsupported baseline provider {baseline!r}')
    baseline_column = baseline + '_id'
    schedule = json.loads((source / 'schedule.json').read_text(encoding='utf-8')).get('events', [])
    events = {str(event['id']): event for event in schedule
              if is_qualification_event(event,competition)==competition.endswith(' Qualifiers')}

    if (source / 'player_match_stats.csv').exists():
        stats = pd.read_csv(source / 'player_match_stats.csv', dtype=str, keep_default_na=False)
        required = {'match_id', 'team', 'player_id', 'player_name'}
        if not required.issubset(stats.columns):
            raise ValueError(f'{source.name}/player_match_stats.csv is missing ID columns')
        stats = stats[stats.match_id.astype(str).isin(events)]
        for record in stats.to_dict('records'):
            player_id = _identifier(record['player_id']);
            team = record['team'].strip()
            if not player_id or not team: continue
            key = (competition, season, team, player_id)
            row = _id_row(player_rows, key, {'competition': competition, 'season': season,
                                             'team_name': team,
                                             'player_name': canonical_player_name(record['player_name'])})
            row['_ids'][baseline_column].append(player_id)

    if (source / 'identities.csv').exists():
        identities = pd.read_csv(source / 'identities.csv', dtype=str, keep_default_na=False)
        required = {'match_id', 'team', 'player_id', 'provider', 'provider_player_id'}
        if not required.issubset(identities.columns):
            raise ValueError(f'{source.name}/identities.csv is missing ID columns')
        identities = identities[identities.match_id.astype(str).isin(events)]
        for record in identities.to_dict('records'):
            provider = record['provider'].strip().casefold()
            if provider not in ('sofascore', 'fotmob', 'whoscored'): continue
            player_id = _identifier(record['player_id']);
            team = record['team'].strip()
            key = (competition, season, team, player_id)
            if key not in player_rows: continue
            provider_id = _reviewed_provider_player_id(
                provider, player_id, record['provider_player_id'])
            if not provider_id: continue
            player_rows[key]['_ids'][provider + '_id'].append(provider_id)

    for event in events.values():
        match_id = _identifier(event.get('id'))
        if match_id:
            timestamp = event.get('startTimestamp')
            date = (datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat()
                    if timestamp is not None else '')
            row = _id_row(match_rows, (competition, season, baseline, match_id), {
                'competition': competition, 'season': season, 'date': date,
                'home_team_name': str((event.get('homeTeam') or {}).get('name') or '').strip(),
                'away_team_name': str((event.get('awayTeam') or {}).get('name') or '').strip()})
            row['_ids'][baseline_column].append(match_id)
        for side in ('home', 'away'):
            team = event.get(side + 'Team') or {};
            team_id = _identifier(team.get('id'))
            if not team_id: continue
            key = (competition, season, team_id)
            row = _id_row(team_rows, key, {'competition': competition, 'season': season,
                                           'team_name': str(team.get('name') or '').strip()})
            row['_ids'][baseline_column].append(team_id)
            for provider in ('fotmob', 'whoscored'):
                reviewed = REVIEWED_TEAM_IDS.get((provider, team_id))
                if reviewed: row['_ids'][provider + '_id'].append(reviewed)

    # Fixture indexes cover matches whose detailed response or player lineup is
    # unavailable, so they are the most complete source of match and team IDs.
    for provider in ('fotmob', 'whoscored'):
        fixtures = _cached_provider_fixtures(source, provider)
        for event in events.values():
            try:
                fixture = match_fixture(event, fixtures, provider=provider, review_root=review_root, baseline=baseline)
            except IdentityError:
                continue
            match_id = _identifier(event.get('id'))
            match_row = match_rows.get((competition, season, baseline, match_id))
            if match_row is not None:
                match_row['_ids'][provider + '_id'].append(fixture.get('id'))
            for side in ('home', 'away'):
                baseline_team = event.get(side + 'Team') or {}
                key = (competition, season, _identifier(baseline_team.get('id')))
                team_row = team_rows.get(key)
                if team_row is not None:
                    team_row['_ids'][provider + '_id'].append(
                        (fixture.get(side + 'Team') or {}).get('id'))

    if _checkpoint_id_rows(source,events,competition,season,baseline,player_rows,team_rows,match_rows):return
    store = PandasStore(source / 'dataset')
    if not store.exists: return
    directory = store.current_directory
    teams = pd.read_csv(directory / 'teams.csv', dtype=str, keep_default_na=False,
                        usecols=['id', 'name'])
    team_names = {_identifier(row.id): json.loads(row.name) for row in teams.itertuples()}
    matches = pd.read_csv(directory / 'matches.csv', dtype=str, keep_default_na=False,
                          usecols=['id', 'home_team_id', 'away_team_id', 'metadata'])
    for match in matches.itertuples():
        if _identifier(match.id) not in events: continue
        metadata = json.loads(match.metadata)
        if is_qualification_event(metadata.get('event') or {},competition) and not competition.endswith(' Qualifiers'): continue
        match_key = (competition, season, baseline, _identifier(match.id))
        match_row = match_rows.get(match_key)
        if match_row is not None:
            provider_ids = metadata.get('provider_match_ids') or {}
            for provider in ('sofascore', 'fotmob', 'whoscored'):
                match_row['_ids'][provider + '_id'].append(provider_ids.get(provider))
        for side in ('home', 'away'):
            team_id = _identifier(getattr(match, side + '_team_id'))
            key = (competition, season, team_id)
            row = _id_row(team_rows, key, {'competition': competition, 'season': season,
                                           'team_name': team_names.get(team_id, '')})
            row['_ids'][baseline_column].append(team_id)
            for provider in ('fotmob', 'whoscored'):
                row['_ids'][provider + '_id'].append(
                    _provider_team_id(source, metadata, provider, side))


def build_id_tables(data_root, output_root, *, build_player_ids=True, build_team_ids=True,
                    build_match_ids=True):
    """Write combined provider ID tables at the top level of ``output_root``."""
    if not build_player_ids and not build_team_ids and not build_match_ids: return {}
    sources = discover_collections(Path(data_root), 'config.json')
    if not sources: raise ValueError(f'No saved collections found under {Path(data_root)}')
    player_rows = {};
    team_rows = {};
    match_rows = {}
    for number, source in enumerate(sources, 1):
        print(f'Indexing IDs [{number}/{len(sources)}]: {source.parent.name}/{source.name}', flush=True)
        _source_id_rows(source, player_rows, team_rows, match_rows, data_root)
    _backfill_unambiguous_ids(player_rows);
    _backfill_unambiguous_ids(team_rows)
    output = Path(output_root);
    pending = {}
    if build_player_ids:
        from providers.teamProfiles import canonical_frame
        players = _finalize_id_rows(player_rows, PLAYER_ID_COLUMNS,
                                    ['competition', 'season', 'team_name', 'player_name'])
        players=canonical_frame(players)
        pending['player IDs'] = (players, output / 'player_ids.csv')
    if build_team_ids:
        from providers.teamProfiles import canonical_team_id,canonical_record
        for row in team_rows.values():
            row.update(canonical_record(row,reference=True))
            for column,values in row.get('_ids',{}).items():
                provider=column.removesuffix('_id')
                if provider in ('sofascore','fotmob','whoscored'):
                    row['_ids'][column]=[canonical_team_id(provider,value) for value in values if value is not None]
        _backfill_unambiguous_ids(team_rows)
        source_teams = _finalize_id_rows(team_rows, SOURCE_TEAM_ID_COLUMNS,
                                         ['competition', 'season', 'team_name'])
        teams = _team_reference(source_teams, output / 'fixtures.csv')
        pending['team IDs'] = (teams, output / 'team_ids.csv')
    if build_match_ids:
        from providers.teamProfiles import canonical_frame
        matches = _finalize_id_rows(match_rows, MATCH_ID_COLUMNS,
                                    ['competition', 'season', 'date', 'home_team_name', 'away_team_name'])
        matches=canonical_frame(matches)
        pending['match IDs'] = (matches, output / 'match_ids.csv')
    output.mkdir(parents=True, exist_ok=True);
    from fixtures.sharedIds import assign_ids,REFERENCE_KEYS
    records={kind:pending[kind+' IDs'][0].fillna('').to_dict('records')
             for kind in ('player','team','match') if kind+' IDs' in pending}
    assign_ids(data_root,[],records.get('match',[]),records.get('team',[]),records.get('player',[]),folder=output/'concordance')
    for kind,rows in records.items():
        frame,path=pending[kind+' IDs']
        columns=list(dict.fromkeys([*frame.columns,*REFERENCE_KEYS,'reep_id','identity_status']))
        pending[kind+' IDs']=(pd.DataFrame(rows,columns=columns),path)
    written = {}
    for label, (frame, path) in pending.items():
        atomic_csv(frame, path);
        written[label] = path
        if label == 'team IDs':
            missing = frame[['sofascore_id', 'whoscored_id']].fillna('').eq('')
            incomplete = int(missing.any(axis=1).sum())
            print(f'Team IDs: {len(frame)} FotMob teams; '
                  f'{len(frame) - incomplete} have all three IDs; '
                  f'{incomplete} have missing provider IDs '
                  f'(SofaScore: {int(missing.sofascore_id.sum())}, '
                  f'WhoScored: {int(missing.whoscored_id.sum())}).', flush=True)
    frames = {kind: pending[kind + ' IDs'][0] for kind in ('player','team','match')
              if kind + ' IDs' in pending}
    written.update(export_concordance(frames, data_root, output,
                                     source_teams=source_teams if build_team_ids else None))
    return written


def export_concordance(frames, data_root, output, *, source_teams=None):
    """Join validated provider tables to Reep without rereading raw responses."""
    from providers.reep import index
    from storage.pandasStore import atomic_json
    bridges = index(data_root)
    concordance = Path(output) / 'concordance'
    written = {}
    manifest = {'format':'soccer-concordance-v1','reep':bridges.metadata,'tables':{}}
    tables={}
    for kind in ('player','team','match'):
        if kind not in frames:
            continue
        frame = frames[kind]
        columns = list(frame.columns)
        if kind == 'team' and source_teams is not None:
            frame = pd.concat([frame, source_teams[list(TEAM_ID_COLUMNS)]], ignore_index=True)
        records = []
        seen = set()
        for record in frame.to_dict('records'):
            clean = {k:'' if pd.isna(v) else str(v) for k,v in record.items()}
            if kind=='team':
                from providers.teamScope import keep_reference
                if not keep_reference(clean):continue
            resolved = bridges.resolve(kind, clean)
            if kind == 'team':
                from providers.teamProfiles import canonical_record
                resolved=canonical_record(resolved,reference=True)
                key = (resolved['reep_id'],) if resolved['reep_id'] and resolved['identity_status']=='linked' else tuple(resolved[p+'_id'] for p in ('sofascore','fotmob','whoscored'))
                if key in seen:
                    continue
                seen.add(key)
            records.append(resolved)
        tables[kind]=(columns,records)
    from fixtures.sharedIds import assign_ids,REFERENCE_KEYS
    detail=assign_ids(data_root,[],tables.get('match',([],[]))[1],tables.get('team',([],[]))[1],
        tables.get('player',([],[]))[1],folder=concordance)
    manifest.update(detail)
    for kind,(columns,records) in tables.items():
        exported = pd.DataFrame(records, columns=list(dict.fromkeys([*columns,'reep_id','identity_status',*REFERENCE_KEYS])))
        path = concordance / (kind + '_ids.csv')
        atomic_csv(exported, path)
        written[kind + ' concordance'] = path
        manifest['tables'][kind] = {'rows':len(exported),
            'reep_linked':int(exported.reep_id.ne('').sum()),
            'conflicts':int(exported.identity_status.eq('conflict').sum()),
            'missing_ids':{p:int(exported[p+'_id'].eq('').sum()) for p in ('sofascore','fotmob','whoscored')}}
    if 'team' in frames:
        from providers.teamHistory import export_history
        path,detail=export_history(data_root,output)
        written['team history concordance']=path
        manifest['team_history']=detail
    atomic_json(concordance / 'manifest.json', manifest)
    return written


def _atomic_copy(source, destination, season=None, *, baseline=None):
    destination = Path(destination);
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid4().hex + '.tmp')
    try:
        if season is None:
            with Path(source).open('rb') as incoming, temporary.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush();
                os.fsync(outgoing.fileno())
        else:
            with Path(source).open('r', encoding='utf-8', newline='') as incoming, \
                    temporary.open('w', encoding='utf-8', newline='') as outgoing:
                reader = csv.reader(incoming);
                writer = csv.writer(outgoing)
                header = next(reader, None)
                if not header or 'season' not in header:
                    raise ValueError(f'{Path(source).name} has no season column')
                index = header.index('season');
                writer.writerow(header)
                for row in reader:
                    if len(row) != len(header): raise ValueError(f'Malformed row in {Path(source).name}')
                    row[index] = season;
                    if baseline is not None:
                        from providers.teamProfiles import canonical_record
                        normalized=canonical_record(dict(zip(header,row)),baseline)
                        row=[normalized.get(key,'') for key in header]
                    writer.writerow(row)
                outgoing.flush();
                os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _score(event, side):
    from providers.scores import final_score
    return final_score(event,side)

def fixtures_from_events(events, competition, season, *, include_qualifying=False, baseline='sofascore'):
    """Convert schedule events using the same schema as packaged fixtures.

    The usual season export omits qualifying matches. A fixtures-only backfill
    can opt in and labels those rows separately without changing their IDs.
    """
    rows = []
    for event in events:
        qualifying = is_qualification_event(event,competition)
        if qualifying and not include_qualifying: continue
        kickoff = datetime.fromtimestamp(event['startTimestamp'], timezone.utc)
        round_info = event.get('roundInfo') or {}
        venue = event.get('venue') or {}
        status = event.get('status') or {}
        phase=competition_phase(competition,event)
        rows.append({
            'competition_phase':phase,
            'competition_note':('Bundesliga promotion/relegation playoff, included in this season; FotMob competition 9081; WhoScored tournament 388.' if phase=='playoffs' and competition=='Bundesliga' else 'Promotion/relegation or promotion playoff, included in this season.' if phase=='playoffs' else ''),
            'match_id': event['id'], 'date': kickoff.date().isoformat(),
            'kickoff_utc': kickoff.isoformat(), 'season': season_label(season),
            'competition': (competition + ' Qualification' if qualifying and not competition.endswith(' Qualifiers') else competition),
            'matchweek': round_info.get('round'),
            'round_name': round_info.get('name'), 'status': status.get('type'),
            'awarded': bool(event.get('isAwarded') or status.get('awarded')),
            'home_team_id': event.get('homeTeam', {}).get('id'),
            'home_team': event.get('homeTeam', {}).get('name'),
            'away_team_id': event.get('awayTeam', {}).get('id'),
            'away_team': event.get('awayTeam', {}).get('name'),
            'home_score': _score(event, 'home'), 'away_score': _score(event, 'away'),
            'winner_code': event.get('winnerCode'), 'venue': venue.get('name'),
            'city': venue.get('city', {}).get('name'),
        })
    from providers.teamProfiles import canonical_frame
    return canonical_frame(pd.DataFrame(rows, columns=FIXTURE_COLUMNS),baseline)


def _fixtures(source, config):
    events = json.loads((source / 'schedule.json').read_text(encoding='utf-8'))['events']
    return fixtures_from_events(events, config['competition'], config['season'],
                                include_qualifying=config['competition'].endswith(' Qualifiers'),
                                baseline=config.get('id_namespace',config.get('baseline_provider','sofascore')))


def package_collection(source, output_root, *, replace=False, tables=None):
    source = Path(source);
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    tables = tuple(SEASON_TABLES) if tables is None else tuple(tables)
    if not tables or set(tables) - set(SEASON_TABLES):
        raise ValueError('Choose at least one valid season table')
    source_files={name:SEASON_TABLES[name][0] for name in tables}
    if 'team_match_stats' in tables and (source/'team_match_statistics.csv').is_file():
        source_files['team_match_stats']='team_match_statistics.csv'
    required = set(source_files.values())
    missing = [name for name in sorted(required) if not (source / name).exists()]
    if missing: raise FileNotFoundError(f'{source.name}: missing ' + ', '.join(missing))
    destination = (Path(output_root) / _slug(config['competition']) /
                   _season_directory(config['season']))
    season = _season_directory(config['season'])
    target = destination
    staging = None;
    backup = None
    if replace:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + '.' + uuid4().hex + '.staging')
        staging.mkdir()
        target = staging
    try:
        # Retain disabled tables from the previous dataset exactly as they were.
        if replace and destination.exists():
            for name, (_, filename) in SEASON_TABLES.items():
                if name not in tables and (destination / filename).is_file():
                    _atomic_copy(destination / filename, target / filename)
        for name in tables:
            _, filename = SEASON_TABLES[name]
            source_name=source_files[name]
            if name == 'fixtures':
                atomic_csv(_fixtures(source, config), target / filename)
            else:
                _atomic_copy(source / source_name, target / filename, season,
                             baseline=config.get('id_namespace',config.get('baseline_provider','sofascore')))
        if replace:
            backup = destination.with_name(destination.name + '.' + uuid4().hex + '.backup')
            if destination.exists(): os.replace(destination, backup)
            try:
                os.replace(staging, destination);
                staging = None
            except Exception:
                if backup.exists(): os.replace(backup, destination)
                raise
            if backup.exists(): shutil.rmtree(backup)
    finally:
        if staging is not None and staging.exists(): shutil.rmtree(staging)
    return destination


# Dataset choices and paths live in config/settings.yaml.

def _build_settings():
    flags = ('BUILD_EVENTS', 'BUILD_PLAYER_MATCH_STATS', 'BUILD_TEAM_MATCH_STATS',
             'BUILD_FIXTURES', 'BUILD_PLAYER_IDS', 'BUILD_TEAM_IDS', 'BUILD_MATCH_IDS',
             'REFRESH_PUBLISHED_TEAM_IDS', 'STANDARDIZE_FIXTURE_TEAM_NAMES')
    values = {name: getattr(config, name) for name in flags}
    if any(type(value) is not bool for value in values.values()):
        raise ValueError('Dataset switches in config/settings.yaml must be True or False')
    tables = tuple(name for name, flag in (
        ('events', 'BUILD_EVENTS'), ('player_match_stats', 'BUILD_PLAYER_MATCH_STATS'),
        ('team_match_stats', 'BUILD_TEAM_MATCH_STATS'), ('fixtures', 'BUILD_FIXTURES'))
        if values[flag])
    if not tables and not any(values[name] for name in ('BUILD_PLAYER_IDS', 'BUILD_TEAM_IDS', 'BUILD_MATCH_IDS')):
        raise ValueError('Enable at least one dataset type in config/settings.yaml')
    if values['STANDARDIZE_FIXTURE_TEAM_NAMES'] and not values['BUILD_TEAM_IDS']:
        raise ValueError('Enable BUILD_TEAM_IDS before standardizing fixtures')
    return tables, values


def _build_reference_csvs(root, output, settings, *, offline=False):
    if settings['REFRESH_PUBLISHED_TEAM_IDS'] and settings['BUILD_TEAM_IDS'] and not offline:
        from providers.reep import refresh
        value = refresh(root, output, config.PROJECT_ROOT / 'ensemble_data')
        print(f"Reep {value['release']}: retained {len(value['rows'])} typed bridges.", flush=True)
    written = build_id_tables(root, output,
                             build_player_ids=settings['BUILD_PLAYER_IDS'],
                             build_team_ids=settings['BUILD_TEAM_IDS'],
                             build_match_ids=settings['BUILD_MATCH_IDS'])
    for label, path in written.items():
        print(f'Wrote combined {label}: {path}', flush=True)
    if settings['STANDARDIZE_FIXTURE_TEAM_NAMES']:
        standardize_fixture_team_names(output / 'fixtures.csv', output / 'team_ids.csv')
    return written


def build_dataset(data_root=None, dataset_root=None, names=None, *, references_only=False, offline=False):
    """Build types enabled in config/settings.yaml from saved local collection data."""
    from collection.cli import main as collect
    tables, settings = _build_settings()
    if references_only:
        tables = ()
    root = Path(config.DATA_ROOT if data_root is None else data_root).expanduser().resolve()
    output = Path(config.DATASET_ROOT if dataset_root is None else dataset_root).expanduser().resolve()
    if root == output or root in output.parents or output in root.parents:
        raise ValueError('Data and dataset roots must be separate directories')
    if settings['STANDARDIZE_FIXTURE_TEAM_NAMES'] and not (output / 'fixtures.csv').is_file():
        raise ValueError('Standardizing fixture names requires dataset/fixtures.csv from the fixture builder')
    reference_names = [name.removeprefix('BUILD_').lower() for name in
                       ('BUILD_PLAYER_IDS', 'BUILD_TEAM_IDS', 'BUILD_MATCH_IDS') if settings[name]]
    print('Dataset types: ' + ', '.join([*tables, *reference_names]), flush=True)
    failures = []
    if tables:
        requested = config.COLLECTION_NAMES if names is None else names
        selected = (list(requested) if requested else
                    [collection_name(path) for path in discover_collections(root, 'job.json')])
        if not selected:
            raise ValueError(f'No saved seasons found under {root}')
        selected = list(dict.fromkeys(selected))
        for name in selected:
            if not (collection_directory(root, name) / 'job.json').is_file():
                raise ValueError(f'Unknown saved season: {name}')
        for index, name in enumerate(selected, 1):
            print(f'[{index}/{len(selected)}] Building dataset: {name}', flush=True)
            try:
                # Fixture-only builds read schedules without exporting other CSVs.
                if any(name != 'fixtures' for name in tables):
                    code = collect(['export', '--data-root', str(root), '--name', name,
                                    '--compile-events' if 'events' in tables else '--no-compile-events'])
                    if code:
                        failures.append(name)
                        continue
                destination = package_collection(collection_directory(root, name), output,
                                                 replace=True, tables=tables)
                print(f'Packaged dataset: {destination}', flush=True)
            except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
                print(f'Failed dataset for {name}: {exc}', flush=True)
                failures.append(name)
    if not failures:
        try:
            _build_reference_csvs(root, output, settings, offline=offline)
        except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
            print(f'Failed combined ID tables: {exc}', flush=True)
            failures.append('ID tables')
    return 2 if failures else 0


def package_dataset(data_root, dataset_root, names=None, *, references_only=False):
    """Package this computer's saved seasons and the existing R Parquet inputs."""
    from concordance.parquet import refresh, folder, validate
    root = Path(data_root).expanduser().resolve()
    output = Path(dataset_root).expanduser().resolve()
    if root == output or root in output.parents or output in root.parents:
        raise ValueError('Data and dataset roots must be separate directories')
    # Local packaging is independent of the worker plan/current season defaults.
    selected = (list(names) if names is not None else
                [collection_name(path) for path in discover_collections(root, 'job.json')])
    if selected or references_only:
        code = build_dataset(root, output, selected, references_only=references_only, offline=True)
        if code: return code
    elif not (output / 'fixtures.csv').is_file() and not (folder(output) / 'manifest.json').is_file():
        raise ValueError(f'No saved seasons or compiled fixtures found under {root} and {output}')
    if refresh(root, output, report=print) is None:
        raise ValueError('No entity-keyed compiled fixture history found; collect or receive fixtures first')
    validate(folder(output))
    print(f'Packaged local dataset: {output}', flush=True)
    print(f'R Parquet concordances: {folder(output)}', flush=True)
    return 0


def main(argv=None, *, package=False):
    # Press Run in PyCharm: no arguments are needed. Optional CLI overrides
    # remain available for existing automated callers.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--dataset-root', type=Path, default=config.DATASET_ROOT)
    parser.add_argument('--name', action='append')
    parser.add_argument('--references-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        build = package_dataset if package else build_dataset
        return build(args.data_root, args.dataset_root,
                     args.name, references_only=args.references_only)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'Dataset build failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())


"""Package player statistics and fixtures as dataset/<league>/<season>/.

This is an offline packaging step. It never scrapes, changes checkpoints, or
modifies files under data/. Run build_csvs.py first when source CSVs are stale.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

import pandas as pd

from collection.runner import atomic_csv
from collection.paths import collection_directory, discover_collections
from collection.selection import is_qualification_event, season_label
from collection.jsonFiles import read_json
from api.events import fotmob_fixture, whoscored_fixture
from api.identity import (IdentityError, REJECTED_PLAYER_ID_LINKS,
                          REVIEWED_PROVIDER_PLAYER_ID_ALIASES, REVIEWED_TEAM_IDS,
                          canonical_player_name, match_fixture, name_key, team_key)
from api.team_crosswalk import VERIFIED_TEAM_ID_OVERRIDES
from storage.pandasStore import PandasStore, atomic_json


SOURCE_FILES = ('player_match_stats.csv', 'schedule.json', 'config.json')
PLAYER_ID_COLUMNS = ('competition','season','team_name','player_name',
                     'sofascore_id','fotmob_id','whoscored_id')
TEAM_ID_COLUMNS = ('team_name','sofascore_id','fotmob_id','whoscored_id')
SOURCE_TEAM_ID_COLUMNS = ('competition','season',*TEAM_ID_COLUMNS)
MATCH_ID_COLUMNS = ('competition','season','date','home_team_name','away_team_name',
                    'sofascore_id','fotmob_id','whoscored_id')
PLAYER_PROVIDER_ID_COLUMNS = ('sofascore_id','fotmob_id','whoscored_id')
FIXTURE_COLUMNS = (
    'match_id','date','kickoff_utc','season','competition','matchweek','round_name',
    'status','awarded','home_team_id','home_team','away_team_id','away_team',
    'home_score','away_score','winner_code','venue','city')
TEAM_NAME_OVERRIDES = {'9788':'Borussia Mönchengladbach'}
TEAM_CROSSWALK = Path(__file__).resolve().parent/'api'/'team_id_crosswalk.json'


def _slug(value):
    return re.sub(r'[^a-z0-9]+', '-', str(value).casefold()).strip('-')


def _season_directory(value):
    found=re.fullmatch(r'(\d{4})/(\d{4})',str(value))
    if not found:raise ValueError(f'Unexpected canonical season {value!r}')
    return f'{found.group(1)}-{found.group(2)}'


def _identifier(value):
    if value is None or value is pd.NA:return ''
    if isinstance(value,float):
        if pd.isna(value):return ''
    text=str(value).strip()
    if text.casefold() in ('','na','n/a','null','nan','none','<na>'):return ''
    if re.fullmatch(r'\d+\.0+',text):text=text.split('.')[0]
    if not text.isdigit() or int(text)<=0:raise ValueError(f'Invalid provider ID: {text!r}')
    return str(int(text))


def _chosen(values):
    """Keep unknown IDs blank and reject contradictory observed IDs."""
    values={_identifier(value) for value in values} - {''}
    if len(values)>1:
        raise ValueError('Conflicting provider IDs: '+', '.join(sorted(values)))
    return next(iter(values)) if values else ''


def _reviewed_provider_player_id(provider,baseline_id,provider_id):
    provider_id=_identifier(provider_id);baseline_id=_identifier(baseline_id)
    if (provider,provider_id,baseline_id) in REJECTED_PLAYER_ID_LINKS:return ''
    return REVIEWED_PROVIDER_PLAYER_ID_ALIASES.get((provider,provider_id),provider_id)


def _id_row(rows,key,base):
    return rows.setdefault(key,{**base,'_ids':{name:[] for name in
        ('sofascore_id','fotmob_id','whoscored_id')}})


def _backfill_unambiguous_ids(rows):
    """Share stable provider IDs across seasons only when the component is unique."""
    entries=list(rows.values());parents=list(range(len(entries)))

    def find(index):
        while parents[index]!=index:
            parents[index]=parents[parents[index]];index=parents[index]
        return index

    def union(left,right):
        left=find(left);right=find(right)
        if left!=right:parents[right]=left

    owners={}
    for index,row in enumerate(entries):
        for column,values in row['_ids'].items():
            for value in {_identifier(item) for item in values} - {''}:
                token=(column,value)
                if token in owners:union(index,owners[token])
                else:owners[token]=index
    components={}
    for index in range(len(entries)):components.setdefault(find(index),[]).append(index)
    for indexes in components.values():
        for column in ('sofascore_id','fotmob_id','whoscored_id'):
            values={_identifier(value) for index in indexes
                    for value in entries[index]['_ids'][column]} - {''}
            if len(values)!=1:continue
            value=next(iter(values))
            for index in indexes:entries[index]['_ids'][column].append(value)


def _finalize_id_rows(rows,columns,sort_columns):
    result=[]
    for row in rows.values():
        values={name:_chosen(items) for name,items in row.pop('_ids').items()}
        result.append({**row,**values})
    frame=pd.DataFrame(result,columns=columns)
    if not frame.empty:frame=frame.sort_values(sort_columns,kind='stable').reset_index(drop=True)
    return frame


def _fixture_provider(path):
    """Read the published fixture namespace; legacy fixture builds used FotMob."""
    path=Path(path);metadata=path.with_suffix('.metadata.json')
    if not metadata.exists():return 'fotmob'
    saved=json.loads(metadata.read_text(encoding='utf-8'))
    provider=saved.get('provider')
    if provider not in ('fotmob','sofascore'):raise ValueError('Unknown fixture ID namespace')
    if saved.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('Fixture CSV differs from its provider metadata; rebuild fixtures first')
    return provider


def _sofascore_team_reference(source_teams,fixtures_path):
    names=_fixture_team_names(fixtures_path);records={}
    for row in source_teams.to_dict('records'):
        sid=_identifier(row.get('sofascore_id'));fid=_identifier(row.get('fotmob_id'))
        key=('sofascore',sid) if sid else ('fotmob',fid)
        if not key[1]:continue
        entry=records.setdefault(key,{'team_name':row['team_name'],**{c:set() for c in PLAYER_PROVIDER_ID_COLUMNS}})
        for column in PLAYER_PROVIDER_ID_COLUMNS:
            identifier=_identifier(row.get(column))
            if identifier:entry[column].add(identifier)
    for sid,name in names.items():
        records.setdefault(('sofascore',sid),{'team_name':name,**{c:set() for c in PLAYER_PROVIDER_ID_COLUMNS}})['sofascore_id'].add(sid)
    result=[]
    for key,row in records.items():
        result.append({'team_name':names.get(key[1],row['team_name']) if key[0]=='sofascore' else row['team_name'],
                       **{column:(_chosen(values) or pd.NA) for column,values in row.items() if column!='team_name'}})
    return pd.DataFrame(result,columns=TEAM_ID_COLUMNS)


def _fixture_team_names(path):
    """Index fixture names by provider ID, never by potentially shared name."""
    names={}
    with Path(path).open(newline='',encoding='utf-8') as stream:
        reader=csv.DictReader(stream)
        required={'home_team_id','home_team','away_team_id','away_team'}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f'{path} is missing fixture team columns')
        for record in reader:
            for side in ('home','away'):
                identifier=_identifier(record[side+'_team_id'])
                name=str(record[side+'_team']).strip()
                if not identifier or not name:
                    raise ValueError(f'{path} contains a blank {side} team ID or name')
                names.setdefault(identifier,Counter())[name]+=1
    return {identifier:TEAM_NAME_OVERRIDES.get(identifier) or
            max(variants,key=lambda name:(len(name),variants[name],name.casefold()))
            for identifier,variants in names.items()}


def _team_crosswalk(path=TEAM_CROSSWALK):
    if not path.exists():return {}
    data=json.loads(path.read_text(encoding='utf-8'))
    return data['teams']


def _team_reference(source_teams,fixtures_path,*,crosswalk_path=TEAM_CROSSWALK):
    """Make a provider-keyed reference, retaining unknown provider IDs as NA."""
    if fixtures_path.exists() and _fixture_provider(fixtures_path)=='sofascore':
        return _sofascore_team_reference(source_teams,fixtures_path)
    names=_fixture_team_names(fixtures_path) if fixtures_path.exists() else {}
    crosswalk=_team_crosswalk(crosswalk_path)
    source={}
    for row in source_teams.to_dict('records'):
        fotmob=_identifier(row['fotmob_id'])
        if not fotmob:continue
        entry=source.setdefault(fotmob,{'names':Counter(),
            'sofascore_id':set(),'whoscored_id':set()})
        if row['team_name']:entry['names'][row['team_name']]+=1
        for column in ('sofascore_id','whoscored_id'):
            value=_identifier(row[column])
            if value:entry[column].add(value)
    records=[];problems=[]
    for fotmob in sorted(set(names)|set(source),key=lambda value:(not value.isdigit(),int(value) if value.isdigit() else value)):
        entry=source.get(fotmob,{})
        observed=entry.get('names',Counter())
        candidate_names=Counter(observed)
        if fotmob in names:candidate_names[names[fotmob]]+=1
        team_name=(TEAM_NAME_OVERRIDES.get(fotmob) or
                   (max(candidate_names,key=lambda name:(len(name),candidate_names[name],
                                                         name.casefold()))
                    if candidate_names else ''))
        published=crosswalk.get(fotmob,{})
        record={'team_name':team_name,'fotmob_id':fotmob}
        for column in ('sofascore_id','whoscored_id'):
            values=set(entry.get(column,set()))
            verified=_identifier(published.get(column))
            if verified:values.add(verified)
            reviewed=_identifier(VERIFIED_TEAM_ID_OVERRIDES.get(fotmob,{}).get(column))
            if reviewed:values.add(reviewed)
            if len(values)>1:
                problems.append(f'{team_name} ({fotmob}) has conflicting {column}: '
                                +', '.join(sorted(values)))
            record[column]=next(iter(values)) if len(values)==1 else pd.NA
        if not team_name or not fotmob:
            missing=[column for column in ('team_name','fotmob_id') if not record[column]]
            problems.append(f'{team_name or "unnamed team"} ({fotmob}) lacks '
                            +', '.join(missing))
        records.append(record)
    for row in source_teams.to_dict('records'):
        if not _identifier(row.get('fotmob_id')):
            identifier=_identifier(row.get('sofascore_id'))
            if not identifier:continue
            linked=[record for record in records if _identifier(record.get('sofascore_id'))==identifier]
            if linked:
                for record in linked:
                    for column in PLAYER_PROVIDER_ID_COLUMNS:
                        record[column]=_chosen([record.get(column),row.get(column)]) or pd.NA
            else:
                records.append({column:(_identifier(row.get(column)) or pd.NA)
                                if column in PLAYER_PROVIDER_ID_COLUMNS else row.get(column)
                                for column in TEAM_ID_COLUMNS})
    if problems:
        preview='\n- '.join(problems)
        raise ValueError('Team identity audit failed; team_ids.csv was not replaced. '
                         f'{len(problems)} issue(s) across {len(records)} FotMob IDs:\n- '
                         +preview)
    return pd.DataFrame(records,columns=TEAM_ID_COLUMNS)


def standardize_fixture_team_names(fixtures_path,team_ids_path):
    """Replace fixture display names using the provider-keyed reference."""
    fixtures_path=Path(fixtures_path);team_ids_path=Path(team_ids_path)
    teams=pd.read_csv(team_ids_path,dtype=str,keep_default_na=False)
    if not set(TEAM_ID_COLUMNS).issubset(teams.columns):
        raise ValueError(f'{team_ids_path} is missing team reference columns')
    provider=_fixture_provider(fixtures_path);column=provider+'_id'
    teams=teams[teams[column].map(_identifier)!=''].copy()
    teams[column]=teams[column].map(_identifier)
    if teams[column].duplicated().any():raise ValueError(f'{team_ids_path} has duplicate {provider} IDs')
    if teams.team_name.str.strip().eq('').any():raise ValueError('Blank team reference name')
    names=dict(zip(teams[column],teams.team_name))
    fixtures=pd.read_csv(fixtures_path,dtype=str,keep_default_na=False)
    if not set(FIXTURE_COLUMNS).issubset(fixtures.columns):
        raise ValueError(f'{fixtures_path} is missing fixture columns')
    for side in ('home','away'):
        missing=set(fixtures[side+'_team_id'])-names.keys()
        if missing:
            raise ValueError(f'{team_ids_path} lacks {len(missing)} fixture '+provider+' '
                             f'IDs, including {", ".join(sorted(missing)[:10])}')
        fixtures[side+'_team']=fixtures[side+'_team_id'].map(names)
    atomic_csv(fixtures,fixtures_path)
    metadata=fixtures_path.with_suffix('.metadata.json')
    if metadata.exists():
        atomic_json(metadata,{'provider':provider,
                    'sha256':hashlib.sha256(fixtures_path.read_bytes()).hexdigest()})


def _validate_provider_links(frame, label):
    """Audit every known pair, including rows with missing provider IDs."""
    columns=[column for column in PLAYER_PROVIDER_ID_COLUMNS if column in frame]
    problems=[]
    for left in columns:
        for right in columns:
            if left==right:continue
            pairs={}
            for record in frame.to_dict('records'):
                a,b=_identifier(record.get(left)),_identifier(record.get(right))
                if a and b:pairs.setdefault(a,set()).add(b)
            for identifier,targets in pairs.items():
                if len(targets)>1:
                    problems.append(f'{left} {identifier} has conflicting ID sets for {right}: '+', '.join(sorted(targets)))
    if problems:
        raise ValueError(f'{label} identity audit failed; existing concordances were not replaced:\n- '+'\n- '.join(problems[:20]))


def _validate_player_identities(frame):
    """Reject conflicting provider links and names, even for partial concordances."""
    if frame.empty:return
    _validate_provider_links(frame,'Player')
    for column in PLAYER_PROVIDER_ID_COLUMNS:
        if column not in frame:continue
        groups={}
        for record in frame.to_dict('records'):
            identifier=_identifier(record.get(column))
            if identifier:
                name=str(record.get('player_name') or '').strip()
                if not name:raise ValueError('Player identity audit failed: blank player name')
                groups.setdefault(identifier,{}).setdefault(name_key(name),set()).add(name)
        for identifier,names in groups.items():
            if len(names)>1:
                display=sorted(name for variants in names.values() for name in variants)
                raise ValueError(f'Player identity audit failed: {column} {identifier} assigned to different names: '+', '.join(display))


def _provider_team_id(source,metadata,provider,side):
    relative=(metadata.get('raw_response_files') or {}).get(provider)
    if not relative:return ''
    path=source/relative
    if not path.exists():return ''
    saved=read_json(path)
    if saved.get('provider')!=provider:
        raise ValueError(f'{path}: expected {provider} response')
    payload=saved.get('payload') or {}
    if provider=='fotmob':
        return _identifier((payload.get('general') or {}).get(side+'Team',{}).get('id'))
    if provider=='whoscored':
        data=payload.get('matchCentreData') or {}
        return _identifier((data.get(side) or {}).get('teamId'))
    return ''


def _cached_provider_fixtures(source,provider):
    converter={'fotmob':fotmob_fixture,'whoscored':whoscored_fixture}[provider]
    fixtures={};folder=Path(source)/'raw'/provider
    for path in folder.glob('*.json') if folder.exists() else ():
        try:
            # Match-detail responses can be large. Their saved envelope puts
            # `key` before `payload`, so skip them without parsing every match.
            with path.open(encoding='utf-8') as stream:
                if not re.search(r'"key"\s*:\s*"fixtures/',stream.read(1024)):
                    continue
            saved=read_json(path)
            if saved.get('provider')!=provider or not str(saved.get('key','')).startswith('fixtures/'):
                continue
            for record in saved.get('payload',[]):
                fixture=converter(record);fixtures[fixture['id']]=fixture
        except (ValueError,KeyError,TypeError):
            continue
    return list(fixtures.values())


def _source_id_rows(source,player_rows,team_rows,match_rows):
    source=Path(source);config=json.loads((source/'config.json').read_text())
    competition=config['competition'];season=_season_directory(config['season'])
    baseline=config.get('id_namespace',config.get('baseline_provider','sofascore'))
    if baseline not in ('sofascore','fotmob','whoscored'):
        raise ValueError(f'{source.name}: unsupported baseline provider {baseline!r}')
    baseline_column=baseline+'_id'
    schedule=json.loads((source/'schedule.json').read_text()).get('events',[])
    events={str(event['id']):event for event in schedule if not is_qualification_event(event)}

    if (source/'player_match_stats.csv').exists():
        stats=pd.read_csv(source/'player_match_stats.csv',dtype=str,keep_default_na=False)
        required={'match_id','team','player_id','player_name'}
        if not required.issubset(stats.columns):
            raise ValueError(f'{source.name}/player_match_stats.csv is missing ID columns')
        stats=stats[stats.match_id.astype(str).isin(events)]
        for record in stats.to_dict('records'):
            player_id=_identifier(record['player_id']);team=record['team'].strip()
            if not player_id or not team:continue
            key=(competition,season,team,player_id)
            row=_id_row(player_rows,key,{'competition':competition,'season':season,
                'team_name':team,
                'player_name':canonical_player_name(record['player_name'])})
            current_name=canonical_player_name(record['player_name'])
            if name_key(row['player_name'])!=name_key(current_name):
                raise ValueError(f'Player identity audit failed: {baseline_column} {player_id} '
                                 f'assigned to different names: {row["player_name"]}, {current_name}')
            row['_ids'][baseline_column].append(player_id)

    if (source/'identities.csv').exists():
        identities=pd.read_csv(source/'identities.csv',dtype=str,keep_default_na=False)
        required={'match_id','team','player_id','provider','provider_player_id'}
        if not required.issubset(identities.columns):
            raise ValueError(f'{source.name}/identities.csv is missing ID columns')
        identities=identities[identities.match_id.astype(str).isin(events)]
        for record in identities.to_dict('records'):
            provider=record['provider'].strip().casefold()
            if provider not in ('sofascore','fotmob','whoscored'):continue
            player_id=_identifier(record['player_id']);team=record['team'].strip()
            key=(competition,season,team,player_id)
            if key not in player_rows:continue
            provider_id=_reviewed_provider_player_id(
                provider,player_id,record['provider_player_id'])
            if not provider_id:continue
            player_rows[key]['_ids'][provider+'_id'].append(provider_id)

    for event in events.values():
        match_id=_identifier(event.get('id'))
        if match_id:
            timestamp=event.get('startTimestamp')
            date=(datetime.fromtimestamp(int(timestamp),timezone.utc).date().isoformat()
                  if timestamp is not None else '')
            row=_id_row(match_rows,(competition,season,baseline,match_id),{
                'competition':competition,'season':season,'date':date,
                'home_team_name':str((event.get('homeTeam') or {}).get('name') or '').strip(),
                'away_team_name':str((event.get('awayTeam') or {}).get('name') or '').strip()})
            row['_ids'][baseline_column].append(match_id)
        for side in ('home','away'):
            team=event.get(side+'Team') or {};team_id=_identifier(team.get('id'))
            if not team_id:continue
            key=(competition,season,team_id)
            row=_id_row(team_rows,key,{'competition':competition,'season':season,
                'team_name':str(team.get('name') or '').strip()})
            row['_ids'][baseline_column].append(team_id)
            for provider in ('fotmob','whoscored'):
                reviewed=REVIEWED_TEAM_IDS.get((provider,team_id))
                if reviewed:row['_ids'][provider+'_id'].append(reviewed)

    # Fixture indexes cover matches whose detailed response or player lineup is
    # unavailable, so they are the most complete source of match and team IDs.
    for provider in ('fotmob','whoscored'):
        fixtures=_cached_provider_fixtures(source,provider)
        for event in events.values():
            try:fixture=match_fixture(event,fixtures)
            except IdentityError:continue
            match_id=_identifier(event.get('id'))
            match_row=match_rows.get((competition,season,baseline,match_id))
            if match_row is not None:
                match_row['_ids'][provider+'_id'].append(fixture.get('id'))
            for side in ('home','away'):
                baseline_team=event.get(side+'Team') or {}
                key=(competition,season,_identifier(baseline_team.get('id')))
                team_row=team_rows.get(key)
                if team_row is not None:
                    team_row['_ids'][provider+'_id'].append(
                        (fixture.get(side+'Team') or {}).get('id'))

    store=PandasStore(source/'dataset')
    if not store.exists:return
    directory=store.current_directory
    teams=pd.read_csv(directory/'teams.csv',dtype=str,keep_default_na=False,
                      usecols=['id','name'])
    team_names={_identifier(row.id):json.loads(row.name) for row in teams.itertuples()}
    matches=pd.read_csv(directory/'matches.csv',dtype=str,keep_default_na=False,
                        usecols=['id','home_team_id','away_team_id','metadata'])
    for match in matches.itertuples():
        if _identifier(match.id) not in events:continue
        metadata=json.loads(match.metadata)
        if is_qualification_event(metadata.get('event') or {}):continue
        match_key=(competition,season,baseline,_identifier(match.id))
        match_row=match_rows.get(match_key)
        if match_row is not None:
            provider_ids=metadata.get('provider_match_ids') or {}
            for provider in ('sofascore','fotmob','whoscored'):
                match_row['_ids'][provider+'_id'].append(provider_ids.get(provider))
        for side in ('home','away'):
            team_id=_identifier(getattr(match,side+'_team_id'))
            key=(competition,season,team_id)
            row=_id_row(team_rows,key,{'competition':competition,'season':season,
                'team_name':team_names.get(team_id,'')})
            row['_ids'][baseline_column].append(team_id)
            for provider in ('fotmob','whoscored'):
                row['_ids'][provider+'_id'].append(
                    _provider_team_id(source,metadata,provider,side))


def _include_fixture_match_ids(fixtures_path,match_rows):
    if not fixtures_path.exists():return
    provider=_fixture_provider(fixtures_path);column=provider+'_id'
    known={}
    for key,row in match_rows.items():
        for value in row['_ids'][column]:
            if _identifier(value):known[_identifier(value)]=key
    frame=pd.read_csv(fixtures_path,dtype=str,keep_default_na=False)
    # Minimal historical team-only fixtures are also accepted by the team builder.
    required={'match_id','competition','season','date','home_team','away_team'}
    if not required.issubset(frame.columns):return
    for record in frame.to_dict('records'):
        if record['competition'].endswith(' Qualification'):continue
        identifier=_identifier(record['match_id'])
        if not identifier:raise ValueError('Fixture row has no match ID')
        key=known.get(identifier,(record['competition'],record['season'],provider,identifier))
        if key in match_rows:
            previous=match_rows[key]
            for field,fixture_field,normalize in (
                    ('date','date',str),('home_team_name','home_team',team_key),
                    ('away_team_name','away_team',team_key)):
                if previous[field] and record[fixture_field] and normalize(previous[field])!=normalize(record[fixture_field]):
                    raise ValueError(f'Match identity audit failed: {column} {identifier} has conflicting fixtures')
        row=_id_row(match_rows,key,{'competition':record['competition'],'season':record['season'],
                    'date':record['date'],'home_team_name':record['home_team'],'away_team_name':record['away_team']})
        row['_ids'][column].append(identifier)


def _validate_match_identities(frame):
    _validate_provider_links(frame,'Match')
    for column in PLAYER_PROVIDER_ID_COLUMNS:
        groups={}
        for record in frame.to_dict('records'):
            identifier=_identifier(record.get(column))
            signature=(record['date'],team_key(record['home_team_name']),team_key(record['away_team_name']))
            if identifier and all(signature):groups.setdefault(identifier,set()).add(signature)
        for identifier,signatures in groups.items():
            if len(signatures)>1:raise ValueError(f'Match identity audit failed: {column} {identifier} has conflicting fixtures')


def build_id_tables(data_root,output_root,*,build_player_ids=True,build_team_ids=True,
                    build_match_ids=True):
    """Write combined provider ID tables at the top level of ``output_root``."""
    if not build_player_ids and not build_team_ids and not build_match_ids:return {}
    sources=discover_collections(Path(data_root),'config.json')
    if not sources and not (Path(output_root)/'fixtures.csv').exists():
        raise ValueError(f'No saved collections or fixtures found under {Path(data_root)}')
    player_rows={};team_rows={};match_rows={}
    for source in sources:_source_id_rows(source,player_rows,team_rows,match_rows)
    if build_match_ids:_include_fixture_match_ids(Path(output_root)/'fixtures.csv',match_rows)
    _backfill_unambiguous_ids(player_rows);_backfill_unambiguous_ids(team_rows)
    output=Path(output_root);pending={}
    if build_player_ids:
        players=_finalize_id_rows(player_rows,PLAYER_ID_COLUMNS,
            ['competition','season','team_name','player_name'])
        _validate_player_identities(players)
        pending['player IDs']=(players,output/'player_ids.csv')
    if build_team_ids:
        source_teams=_finalize_id_rows(team_rows,SOURCE_TEAM_ID_COLUMNS,
            ['competition','season','team_name'])
        _validate_provider_links(source_teams,'Team')
        teams=_team_reference(source_teams,output/'fixtures.csv')
        _validate_provider_links(pd.concat([source_teams,teams],ignore_index=True),'Team')
        pending['team IDs']=(teams,output/'team_ids.csv')
    if build_match_ids:
        matches=_finalize_id_rows(match_rows,MATCH_ID_COLUMNS,
            ['competition','season','date','home_team_name','away_team_name'])
        _validate_match_identities(matches)
        pending['match IDs']=(matches,output/'match_ids.csv')
    output.mkdir(parents=True,exist_ok=True);written={}
    for label,(frame,path) in pending.items():
        atomic_csv(frame,path);written[label]=path
        if label=='team IDs':
            missing=frame[list(PLAYER_PROVIDER_ID_COLUMNS)].isna() | frame[list(PLAYER_PROVIDER_ID_COLUMNS)].eq('')
            incomplete=int(missing.any(axis=1).sum())
            print(f'Team IDs: {len(frame)} teams; '
                  f'{len(frame)-incomplete} have all three IDs; '
                  f'{incomplete} have missing provider IDs '
                  f'(SofaScore: {int(missing.sofascore_id.sum())}, '
                  f'FotMob: {int(missing.fotmob_id.sum())}, '
                  f'WhoScored: {int(missing.whoscored_id.sum())}).',flush=True)
    return written


def _atomic_copy(source,destination,season=None):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.'+uuid4().hex+'.tmp')
    try:
        if season is None:
            with Path(source).open('rb') as incoming,temporary.open('wb') as outgoing:
                shutil.copyfileobj(incoming,outgoing,length=1024*1024)
                outgoing.flush();os.fsync(outgoing.fileno())
        else:
            with Path(source).open('r',encoding='utf-8',newline='') as incoming, \
                    temporary.open('w',encoding='utf-8',newline='') as outgoing:
                reader=csv.reader(incoming);writer=csv.writer(outgoing)
                header=next(reader,None)
                if not header or 'season' not in header:
                    raise ValueError(f'{Path(source).name} has no season column')
                index=header.index('season');writer.writerow(header)
                for row in reader:
                    if len(row)!=len(header):raise ValueError(f'Malformed row in {Path(source).name}')
                    row[index]=season;writer.writerow(row)
                outgoing.flush();os.fsync(outgoing.fileno())
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)


def _score(event,side):
    values=event.get(side+'Score') or {}
    for key in ('display','normaltime','current'):
        if values.get(key) is not None:return values[key]
    return None


def fixtures_from_events(events, competition, season, *, include_qualifying=False):
    """Convert schedule events using the same schema as packaged fixtures.

    The usual season export omits qualifying matches. A fixtures-only backfill
    can opt in and labels those rows separately without changing their IDs.
    """
    rows=[]
    for event in events:
        qualifying=is_qualification_event(event)
        if qualifying and not include_qualifying:continue
        kickoff=datetime.fromtimestamp(event['startTimestamp'],timezone.utc)
        round_info=event.get('roundInfo') or {}
        venue=event.get('venue') or {}
        status=event.get('status') or {}
        rows.append({
            'match_id':event['id'],'date':kickoff.date().isoformat(),
            'kickoff_utc':kickoff.isoformat(),'season':season_label(season),
            'competition':(competition+' Qualification' if qualifying else competition),
            'matchweek':round_info.get('round'),
            'round_name':round_info.get('name'),'status':status.get('type'),
            'awarded':bool(event.get('isAwarded') or status.get('awarded')),
            'home_team_id':event.get('homeTeam',{}).get('id'),
            'home_team':event.get('homeTeam',{}).get('name'),
            'away_team_id':event.get('awayTeam',{}).get('id'),
            'away_team':event.get('awayTeam',{}).get('name'),
            'home_score':_score(event,'home'),'away_score':_score(event,'away'),
            'winner_code':event.get('winnerCode'),'venue':venue.get('name'),
            'city':venue.get('city',{}).get('name'),
        })
    return pd.DataFrame(rows,columns=FIXTURE_COLUMNS)


def _fixtures(source,config):
    events=json.loads((source/'schedule.json').read_text())['events']
    return fixtures_from_events(events,config['competition'],config['season'])


def package_collection(source,output_root,*,replace=False):
    source=Path(source);config=json.loads((source/'config.json').read_text())
    missing=[name for name in SOURCE_FILES if not (source/name).exists()]
    if missing:raise FileNotFoundError(f'{source.name}: missing '+', '.join(missing))
    destination=(Path(output_root)/_slug(config['competition'])/
                 _season_directory(config['season']))
    season=_season_directory(config['season'])
    target=destination
    staging=None;backup=None
    if replace:
        destination.parent.mkdir(parents=True,exist_ok=True)
        staging=destination.with_name(destination.name+'.'+uuid4().hex+'.staging')
        staging.mkdir()
        target=staging
    try:
        _atomic_copy(source/'player_match_stats.csv',target/'player_match_stats.csv',season)
        atomic_csv(_fixtures(source,config),target/'fixtures.csv')
        if replace:
            backup=destination.with_name(destination.name+'.'+uuid4().hex+'.backup')
            if destination.exists():os.replace(destination,backup)
            try:
                os.replace(staging,destination);staging=None
            except Exception:
                if backup.exists():os.replace(backup,destination)
                raise
            if backup.exists():shutil.rmtree(backup)
    finally:
        if staging is not None and staging.exists():shutil.rmtree(staging)
    return destination


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=Path(__file__).parent/'data')
    parser.add_argument('--output-root',type=Path,default=Path(__file__).parent/'dataset')
    parser.add_argument('--name',action='append',help='Saved collection folder; repeat as needed. Omit for all.')
    parser.add_argument('--replace',action='store_true',
                        help='Atomically replace each destination season directory')
    args=parser.parse_args(argv)
    root=args.data_root.resolve();output=args.output_root.resolve()
    sources=([collection_directory(root,name) for name in args.name] if args.name else
             discover_collections(root,'config.json'))
    if not sources:parser.error(f'No saved collections found under {root}')
    seen={};failures=[]
    for index,source in enumerate(sources,1):
        try:
            config=json.loads((source/'config.json').read_text())
            identity=(_slug(config['competition']),_season_directory(config['season']))
            if identity in seen:
                raise ValueError(f'duplicate league-season also provided by {seen[identity]}')
            seen[identity]=str(source.relative_to(root))
            label=str(source.relative_to(root))
            print(f'[{index}/{len(sources)}] Packaging {label}...',flush=True)
            destination=package_collection(source,output,replace=args.replace)
            print(f'[{index}/{len(sources)}] Wrote {destination}',flush=True)
        except (OSError,ValueError,KeyError,json.JSONDecodeError,pd.errors.ParserError) as exc:
            label=str(source.relative_to(root))
            failures.append(label)
            print(f'[{index}/{len(sources)}] FAILED {label}: {exc}',flush=True)
    if failures:print('Failed collections: '+', '.join(failures),flush=True)
    return 2 if failures else 0


if __name__=='__main__':
    raise SystemExit(main())

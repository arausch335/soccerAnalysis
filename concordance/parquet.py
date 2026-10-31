"""Chunked R-facing fixtures and six-ID/name concordances; no provider requests."""
from collections import Counter,defaultdict
from datetime import datetime,timezone,date
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from filelock import FileLock
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from providers.reep import PROVIDERS
from storage.pandasStore import atomic_json,atomic_replace

FORMAT='soccer-concordances-parquet-v1'
CHUNK_ROWS=50_000
ID_COLUMNS=('entity_id','alex_id','reep_id','sofascore_id','fotmob_id','whoscored_id')
NAME_COLUMNS=('entity_name','reep_name','sofascore_name','fotmob_name','whoscored_name')
CORE_COLUMNS=('match_entity_id','date','kickoff_utc','season','competition','matchweek','round_name','status','awarded',
    'home_team_entity_id','home_team','away_team_entity_id','away_team','home_score','away_score','winner_code','has_player_stats')
EXTRA_COLUMNS=('match_entity_id','venue','city','competition_phase','competition_note','competition_type','competition_country','is_scheduled_for_scraping')
RENAMES={'match_entity_id':'entity_id','home_team_entity_id':'home_entity_id','away_team_entity_id':'away_entity_id'}
LOOKUP={**RENAMES,'source_home_team':'home_team','source_away_team':'away_team'}
TABLES=('fixtures/fixtures','fixtures/extra','fixtures/metadata','match_ids','team_ids','player_ids',
        'metadata/match_ids','metadata/team_ids','metadata/player_ids','metadata/registry','entities')


def text(value):
    if value is None or value is pd.NA:return ''
    if isinstance(value,(dict,list,tuple)):return json.dumps(value,ensure_ascii=False,sort_keys=True)
    if isinstance(value,float) and pd.isna(value):return ''
    return str(value)


def flag(value,*,nullable=False):
    value=text(value).lower()
    if not value:return None if nullable else False
    if value not in ('true','false'):raise ValueError('Invalid boolean fixture field: '+value)
    return value=='true'


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024**2),b''):result.update(chunk)
    return result.hexdigest()


def folder(dataset):return Path(dataset)/'concordances'


def exists(dataset):return (folder(dataset)/'manifest.json').is_file()


def read_table(base,name):
    manifest=json.loads((Path(base)/'manifest.json').read_text(encoding='utf-8'))
    files=manifest['tables'][name]['files']
    return pa.concat_tables([pq.read_table(Path(base)/item['path']) for item in files]).to_pandas()


def wide_rows(dataset):
    """Reconstruct the internal linkage view without exposing it as the fixture table."""
    base=folder(dataset)
    core=read_table(base,'fixtures/fixtures').rename(columns=RENAMES)
    extra=read_table(base,'fixtures/extra').rename(columns={'match_entity_id':'entity_id'})
    metadata=read_table(base,'fixtures/metadata').rename(columns={'match_entity_id':'entity_id'})
    result=core.merge(extra,on='entity_id',validate='one_to_one').merge(metadata,on='entity_id',validate='one_to_one')
    for side in ('home','away'):
        key='source_'+side+'_team'
        if key in result:result[side+'_team']=result.pop(key)
    for column in result:
        if column=='date':result[column]=result[column].map(lambda v:v.isoformat() if pd.notna(v) else '')
        elif column=='kickoff_utc':result[column]=result[column].map(lambda v:v.isoformat() if pd.notna(v) else '')
        else:result[column]=result[column].map(text)
    return result.to_dict('records')


def reference_rows(dataset,kind):
    if not exists(dataset):return []
    base=folder(dataset);ids=read_table(base,kind+'_ids');details=read_table(base,'metadata/'+kind+'_ids')
    return ids.merge(details,on='entity_id',how='left',validate='one_to_one').map(text).to_dict('records')


def read_registry(base):
    manifest=json.loads((Path(base)/'manifest.json').read_text(encoding='utf-8'))
    value={'format':manifest['registry_format'],'aliases':{},'published':{},'redirects':{},'anchors':{}}
    for row in read_table(base,'metadata/registry').to_dict('records'):
        value.setdefault(row['section'],{})[row['key']]=json.loads(row['value'])
    return value


def fixture_references(rows):
    matches=[];teams=[]
    for row in rows:
        match={k:row.get(k,'') for k in ('entity_id','reep_id','alex_id','safe_for_cross_provider_joins','identity_status','provider_aliases')}
        match.update(reep_id=row.get('reep_match_id') or row.get('reep_id',''),alex_id=row.get('alex_match_id') or row.get('alex_id',''))
        for p in PROVIDERS:match[p+'_id']=row.get(p+'_match_id') or row.get(p+'_id','')
        for p in ('reep',*PROVIDERS):
            home=row.get(p+'_home_team') or (row.get('home_team') if p==row.get('baseline_provider','sofascore') else '')
            away=row.get(p+'_away_team') or (row.get('away_team') if p==row.get('baseline_provider','sofascore') else '')
            match[p+'_name']=row.get(p+'_name') or (home+' vs '+away if home and away else '')
        matches.append(match)
        for side in ('home','away'):
            entity=row.get(side+'_entity_id')
            if not entity:continue
            item={'entity_id':entity,'alex_id':row.get('alex_'+side+'_team_id',''),
                'reep_id':row.get('reep_'+side+'_team_id',''),'team_name':row.get(side+'_team',''),
                'safe_for_cross_provider_joins':row.get(side+'_safe_for_cross_provider_joins',False)}
            for p in PROVIDERS:item[p+'_id']=row.get(p+'_'+side+'_team_id','')
            for p in ('reep',*PROVIDERS):
                item[p+'_name']=row.get(p+'_'+side+'_team') or (row.get(side+'_team','') if p==row.get('baseline_provider','sofascore') else '')
            teams.append(item)
    return matches,teams


def identities(rows,kind,*,registry=None,published_names=None):
    """One row per identity; all profile/context/check information stays in metadata."""
    groups=defaultdict(list)
    for row in rows:
        if row.get('entity_id'):groups[text(row['entity_id'])].append(row)
    ids=[];details=[]
    for entity,items in sorted(groups.items()):
        result={key:'' for key in (*ID_COLUMNS,*NAME_COLUMNS)};result['entity_id']=entity
        audit={'entity_id':entity};native_aliases=defaultdict(set)
        for key in ID_COLUMNS[1:]:
            values={text(r.get(key)) for r in items if text(r.get(key))}
            if key.endswith('_id') and key[:-3] in PROVIDERS:
                provider=key[:-3]
                values={v for v in values if registry is None or registry.lookup(kind,provider,v)==entity}
                native_aliases[key].update(values)
            if key=='alex_id' and registry is not None:values={registry.follow(v) for v in values}
            if key=='reep_id':values={v for v in values if v==entity}
            result[key]=sorted(values)[0] if values else ''
        if not result['alex_id'] and entity.startswith('a'):result['alex_id']=entity
        if not result['alex_id'] and registry is not None:
            for p in PROVIDERS:
                alias=registry.value['aliases'].get(f'{kind}|{p}|{result[p+"_id"]}')
                if alias and registry.canonical(alias)==entity:
                    result['alex_id']=registry.follow(alias);break
        for provider in ('reep',*PROVIDERS):
            counts=Counter(text(r.get(provider+'_name')) for r in items if text(r.get(provider+'_name')))
            # Generic old catalog names belong to its recorded baseline only.
            if not counts and provider in PROVIDERS:
                for r in items:
                    baseline=r.get('baseline_provider') or next((p for p in PROVIDERS if r.get(p+'_id')),None)
                    name=r.get('player_name') or r.get('team_name')
                    if baseline==provider and name:counts[text(name)]+=1
            if provider=='reep' and published_names and result['reep_id']:
                name=published_names.get(kind,{}).get(result['reep_id'])
                if name:counts[text(name)]+=1_000_000
            if result.get(provider+'_id') and counts:result[provider+'_name']=min(counts,key=lambda v:(-counts[v],v))
        result['entity_name']=next((result[p+'_name'] for p in ('reep',*PROVIDERS) if result[p+'_name']),'')
        fields={k for r in items for k in r}-set(ID_COLUMNS)-set(NAME_COLUMNS)
        for key in sorted(fields):
            values=sorted({text(r.get(key)) for r in items if text(r.get(key))})
            audit[key]=values[0] if len(values)==1 else json.dumps(values,ensure_ascii=False) if values else ''
        for r in items:
            aliases=r.get('provider_aliases')
            if aliases:
                for key,values in json.loads(aliases).items():native_aliases[key].update(values)
        audit['provider_aliases']=json.dumps({k:sorted(v) for k,v in native_aliases.items()},sort_keys=True)
        ids.append(result);details.append(audit)
    return ids,details


def arrow_table(rows,columns,*,typed=False):
    fields=[];values={}
    for column in columns:
        kind=pa.string();data=[r.get(column,r.get(LOOKUP.get(column,column))) for r in rows]
        if typed and column=='date':
            kind=pa.date32();data=[date.fromisoformat(text(v)[:10]) if text(v) else None for v in data]
        elif typed and column=='kickoff_utc':
            kind=pa.timestamp('us',tz='UTC');parsed=pd.to_datetime([text(v) or None for v in data],utc=True,format='ISO8601')
            data=[None if pd.isna(v) else v.to_pydatetime() for v in parsed]
        elif typed and column in ('has_player_stats','is_scheduled_for_scraping','awarded'):
            kind=pa.bool_();data=[flag(v,nullable=column=='awarded') for v in data]
        elif typed and column in ('matchweek','home_score','away_score','winner_code'):
            kind=pa.int64();converted=[]
            for v in data:
                number=float(v) if text(v) else None
                if number is not None and not number.is_integer():raise ValueError('Noninteger fixture field: '+column)
                converted.append(int(number) if number is not None else None)
            data=converted
        else:data=[text(v) or None for v in data]
        fields.append(pa.field(column,kind));values[column]=data
    return pa.Table.from_pydict(values,schema=pa.schema(fields))


def validate(base,*,require_integration=False):
    base=Path(base);manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=FORMAT:raise ValueError('Unknown Parquet concordance format')
    if require_integration and not manifest.get('ready_for_R'):raise ValueError('MSI integration is unconfirmed; audit and rebuild first')
    expected=set()
    for name,info in manifest['tables'].items():
        if name not in TABLES:raise ValueError('Unknown concordance table')
        count=0
        for item in info['files']:
            path=Path(item['path'])
            if path.parent.as_posix()!=name or not re.fullmatch(r'part-\d{5}\.parquet',path.name):raise ValueError('Unsafe Parquet shard path')
            target=base/path;expected.add(path.as_posix())
            if digest(target)!=item['sha256']:raise ValueError('Parquet checksum mismatch: '+str(path))
            count+=pq.read_metadata(target).num_rows
        if count!=info['rows']:raise ValueError('Parquet row count differs')
    if set(manifest['tables'])!=set(TABLES):raise ValueError('Incomplete Parquet table set')
    if {p.relative_to(base).as_posix() for p in base.rglob('*.parquet')}!=expected:raise ValueError('Stale or unverified Parquet shards')
    core=read_table(base,'fixtures/fixtures');extra=read_table(base,'fixtures/extra');meta=read_table(base,'fixtures/metadata')
    if tuple(core.columns)!=CORE_COLUMNS or tuple(extra.columns)!=EXTRA_COLUMNS:raise ValueError('Incorrect fixture schema')
    keys=set(core.match_entity_id)
    for frame in (core,extra,meta):
        if frame.match_entity_id.isna().any() or frame.match_entity_id.duplicated().any() or set(frame.match_entity_id)!=keys:raise ValueError('Fixture partitions do not have the same unique match keys')
    if (core.has_player_stats & extra.set_index('match_entity_id').loc[core.match_entity_id].is_scheduled_for_scraping.to_numpy()).any():raise ValueError('Scraped fixture cannot also be pending')
    from .registry import Registry
    registry=Registry(base/'unused-state',value=read_registry(base))
    mappings={}
    for kind in ('match','team','player'):
        frame=read_table(base,kind+'_ids')
        if tuple(frame.columns)!=(*ID_COLUMNS,*NAME_COLUMNS):raise ValueError('ID table must contain six IDs and five names')
        if frame.entity_id.isna().any() or frame.entity_id.duplicated().any():raise ValueError('Missing or duplicate concordance identity')
        canonical=frame.reep_id.fillna(frame.alex_id)
        if canonical.isna().any() or not frame.entity_id.equals(canonical):raise ValueError('Canonical ID differs from Reep/Alex identity')
        if frame[[p+'_id' for p in PROVIDERS]].isna().all(axis=1).any():raise ValueError('Identity has no real provider ID')
        for p in PROVIDERS:
            known=frame.dropna(subset=[p+'_id'])
            if known[p+'_id'].duplicated().any():raise ValueError('Provider ID maps to different entities')
        chosen=frame[[p+'_name' for p in ('reep',*PROVIDERS)]].bfill(axis=1).iloc[:,0]
        if frame.entity_name.fillna('').tolist()!=chosen.fillna('').tolist():raise ValueError('Entity name does not follow provider priority')
        registry.validate_rows(kind,[{**r,'safe_for_cross_provider_joins':True} for r in frame.map(text).to_dict('records')])
        mappings[kind]=set(frame.entity_id)
    if not keys<=mappings['match']:raise ValueError('Fixture match is absent from match_ids')
    for side in ('home','away'):
        if not set(core[side+'_team_entity_id'].dropna())<=mappings['team']:raise ValueError('Fixture team is absent from team_ids')
    for side in ('home','away'):
        missing=core[side+'_team_entity_id'].isna()
        if missing.any():
            facts=meta.set_index('match_entity_id').reindex(core.match_entity_id)
            allowed=core.has_player_stats.to_numpy() & facts.get('fixture_source',pd.Series('',index=facts.index)).eq('stored_player_stats').to_numpy()
            if (missing.to_numpy() & ~allowed).any():raise ValueError('Fixture is missing a team entity ID')
    return {'format':FORMAT,'rows':len(core),'tables':{k:v['rows'] for k,v in manifest['tables'].items()},'ready_for_R':manifest['ready_for_R']}


def publish(root,dataset,rows,*,matches=(),teams=(),players=(),entities=(),registry=None,confirmed=False,chunk_rows=CHUNK_ROWS,output=None,check=None):
    if not 1<=chunk_rows<=1_000_000:raise ValueError('chunk_rows must be between 1 and 1000000')
    from providers.reep import snapshot
    from .sync import version as registry_version
    root,dataset=Path(root),Path(dataset);target=Path(output) if output is not None else folder(dataset);target.parent.mkdir(parents=True,exist_ok=True)
    rows=list(rows)
    match_rows,team_rows=fixture_references(rows)
    names=(snapshot(root) or {}).get('names',{})
    tables={'fixtures/extra':arrow_table(rows,EXTRA_COLUMNS,typed=True)}
    used={RENAMES.get(k,k) for k in (*CORE_COLUMNS,*EXTRA_COLUMNS)}
    metadata_columns=['match_entity_id','source_home_team','source_away_team',*sorted({k for r in rows for k in r}-used-{'source_home_team','source_away_team','match_entity_id'})]
    tables['fixtures/metadata']=arrow_table(rows,metadata_columns)
    for kind,items in [('match',[*matches,*match_rows]),('team',[*teams,*team_rows]),('player',list(players))]:
        ids,details=identities(items,kind,registry=registry,published_names=names)
        tables[kind+'_ids']=arrow_table(ids,(*ID_COLUMNS,*NAME_COLUMNS))
        tables['metadata/'+kind+'_ids']=arrow_table(details,['entity_id',*sorted({k for r in details for k in r}-{'entity_id'})])
    canonical_names={r['entity_id']:r['entity_name'] for r in tables['team_ids'].to_pylist()}
    core_rows=[{**{c:r.get(RENAMES.get(c,c)) for c in CORE_COLUMNS},**{side+'_team':canonical_names.get(r.get(side+'_entity_id')) or r.get(side+'_team','') for side in ('home','away')}} for r in rows]
    tables['fixtures/fixtures']=arrow_table(core_rows,CORE_COLUMNS,typed=True)
    entities=list(entities);tables['entities']=arrow_table(entities,sorted({k for r in entities for k in r}) or ['relationship_id','from_entity_id','to_entity_id'])
    state=registry.value if registry else {'format':'soccer-alex-ids-v1','aliases':{},'published':{},'redirects':{},'anchors':{}}
    registry_rows=[{'section':section,'key':key,'value':json.dumps(value,ensure_ascii=False,sort_keys=True)}
        for section,values in sorted(state.items()) if isinstance(values,dict) for key,value in sorted(values.items())]
    tables['metadata/registry']=arrow_table(registry_rows,['section','key','value'])
    with FileLock(str(target.parent/'.concordances.lock'),timeout=25):
        with tempfile.TemporaryDirectory(prefix='.concordances-',dir=target.parent) as temporary:
            stage=Path(temporary)/'bundle';stage.mkdir();manifest={'format':FORMAT,'built_at':datetime.now(timezone.utc).isoformat(),
                'registry_format':state['format'],'registry_version':registry_version(registry.value) if registry else '',
                'ready_for_R':bool(confirmed),'integration_confirmed':bool(confirmed),'chunk_rows':chunk_rows,'tables':{},
                'name_priority':['reep','sofascore','fotmob','whoscored']}
            for name,table in tables.items():
                info={'rows':len(table),'files':[]};directory=stage/name;directory.mkdir(parents=True)
                for part,start in enumerate(range(0,max(1,len(table)),chunk_rows)):
                    path=directory/f'part-{part:05d}.parquet';pq.write_table(table.slice(start,chunk_rows),path,compression='zstd')
                    info['files'].append({'path':path.relative_to(stage).as_posix(),'sha256':digest(path)})
                manifest['tables'][name]=info
            atomic_json(stage/'manifest.json',manifest,indent=2);validate(stage)
            if check:check()
            previous=target.parent/('.concordances-previous-'+uuid4().hex)
            if target.exists():atomic_replace(target,previous)
            try:atomic_replace(stage,target)
            except BaseException:
                if previous.exists():atomic_replace(previous,target)
                raise
            if previous.exists():shutil.rmtree(previous)
    return manifest


def compiled_signature(root,dataset):
    paths=[Path(root)/'_system/identity/alex_ids.json',Path(root)/'_system/identity/reep.json',
           Path(dataset)/'fixtures.csv',folder(dataset)/'manifest.json',Path(dataset)/'concordance/team_history.csv']
    paths.extend(Path(dataset)/parent/(kind+'_ids.csv') for parent in ('fixture_concordance','concordance','') for kind in ('match','team','player'))
    return [(str(p),p.stat().st_size,p.stat().st_mtime_ns) if p.is_file() else (str(p),None,None) for p in paths]


def refresh(root,dataset,*,rows=None,confirmed=False,chunk_rows=CHUNK_ROWS,output=None,report=None):
    """Publish already compiled fixtures/references; never start a collection scan."""
    import csv
    from .registry import Registry
    root,dataset=Path(root),Path(dataset);before=compiled_signature(root,dataset)
    if rows is None:
        path=dataset/'fixtures.csv'
        if path.is_file():
            with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
        elif exists(dataset):rows=wide_rows(dataset)
        else:return None
    if any(not row.get('entity_id') for row in rows):return None
    if report:report(f'Read {len(rows)} compiled fixtures',flush=True)
    registry_path=root/'_system/identity/alex_ids.json'
    registry=Registry(registry_path) if registry_path.is_file() else None
    if report:report('Loaded the saved identity registry',flush=True)
    references={kind:[] for kind in ('match','team','player')}
    for kind in references:
        for parent in ('fixture_concordance','concordance',''):
            path=dataset/parent/(kind+'_ids.csv')
            if path.is_file():
                with path.open(encoding='utf-8-sig',newline='') as stream:references[kind].extend(csv.DictReader(stream))
        if exists(dataset):references[kind].extend(reference_rows(dataset,kind))
        for row in references[kind]:
            if registry and row.get('entity_id'):
                row['entity_id']=registry.canonical(row['entity_id'])
                if row.get('alex_id'):row['alex_id']=registry.follow(row['alex_id'])
                row['reep_id']=row['entity_id'] if row['entity_id'].startswith(('rm','rt','rp')) else ''
    if report:report('Read compiled ID references: '+str({k:len(v) for k,v in references.items()}),flush=True)
    history=dataset/'concordance/team_history.csv';entities=[]
    if history.is_file():
        with history.open(encoding='utf-8-sig',newline='') as stream:entities=list(csv.DictReader(stream))
    def unchanged():
        if compiled_signature(root,dataset)!=before:raise ValueError('Compiled fixtures or identities changed during export; previous Parquet publication preserved; retry')
    return publish(root,dataset,rows,matches=references['match'],teams=references['team'],players=references['player'],
        entities=entities,registry=registry,confirmed=confirmed,chunk_rows=chunk_rows,output=output,check=unchanged)

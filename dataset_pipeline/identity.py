"""Resolve exported native IDs in Python without inventing provider equivalences."""
from collections import Counter,defaultdict
from functools import lru_cache
from pathlib import Path
import csv
import json
import os
import unicodedata

from collection.storageTransfer import digest
from storage.pandasStore import atomic_json

FORMAT='soccer-dataset-identities-v1'
PROVIDERS=('sofascore','fotmob','whoscored')
IDENTITY_FIELDS=('match_id','home_team_id','away_team_id','team_id','opponent_id','player_id','provider','provider_match_id','provider_team_id','side','team','opponent',
                 *(p+'_match_id' for p in PROVIDERS),*(p+'_'+side+'_team_id' for p in PROVIDERS for side in ('home','away')))


def text(value):
    value='' if value is None else str(value).strip()
    return '' if value.lower() in ('','na','nan','none','<na>') else value


def name_key(value):
    return ' '.join(unicodedata.normalize('NFKC',text(value)).casefold().split())


class Resolver:
    def __init__(self,dataset):
        from concordance.publication import folder,read_table,read_registry
        from concordance.registry import Registry
        base=folder(dataset);self.registry=Registry(Path(dataset)/'.unused-registry',value=read_registry(base))
        self.allowed={};self.native={}
        for kind in ('match','team','player'):
            records=read_table(base,kind+'_ids').fillna('').to_dict('records')
            self.allowed[kind]={r['entity_id'] for r in records}
            for row in records:
                for provider in PROVIDERS:
                    native=text(row[provider+'_id'])
                    if native:self.native[kind,provider,native]=row['entity_id']
        self.fixtures={r['match_entity_id']:r for r in read_table(base,'fixtures/fixtures').fillna('').to_dict('records')}
        self.metadata={r['match_entity_id']:r for r in read_table(base,'fixtures/metadata').fillna('').to_dict('records')}
        self.namespaces=defaultdict(set)
        from dataset_pipeline.build import _slug
        for entity,fixture in self.fixtures.items():
            provider=self.metadata[entity].get('baseline_provider')
            key=(_slug(fixture['competition']).replace('-qualification','-qualifiers'),fixture['season'])
            if provider in PROVIDERS:self.namespaces[key].add(provider)
        self.resolve=lru_cache(maxsize=None)(self.resolve)
        self._row=lru_cache(maxsize=100_000)(self._row)

    def resolve(self,kind,provider,native):
        if provider not in PROVIDERS or not text(native):return ''
        native=text(native)
        if native.endswith('.0') and native[:-2].isdigit():native=native[:-2]
        result=self.registry.lookup(kind,provider,native)
        if result in self.allowed[kind]:return result
        return self.native.get((kind,provider,native),'')

    def namespace(self,path,source_root):
        if source_root is not None:
            config=Path(source_root)/path.parent.parent.name/path.parent.name/'config.json'
            if config.is_file():
                settings=json.loads(config.read_text());provider=settings.get('id_namespace',settings.get('baseline_provider','sofascore'))
                if provider not in PROVIDERS:raise ValueError('Unknown native ID namespace: '+str(provider))
                return provider
        values=self.namespaces.get((path.parent.parent.name,path.parent.name),set())
        return next(iter(values)) if len(values)==1 else ''

    def row(self,row,table,baseline):
        values=tuple((k,text(row[k])) for k in IDENTITY_FIELDS if k in row)
        return self._row(values,table,baseline)

    def _row(self,values,table,baseline):
        row=dict(values)
        issues=[];methods={};result={}
        def collect(kind,candidates):
            found=set();used_alias=False
            for provider,number in candidates:
                number=text(number);entity=self.resolve(kind,provider,number)
                if entity:
                    found.add(entity)
                    used_alias|=self.native.get((kind,provider,number))!=entity
            if len(found)>1:return '',True,False
            return next(iter(found)) if found else '',False,used_alias
        candidates=[(baseline,row.get('match_id'))]
        if row.get('provider') in PROVIDERS:candidates.append((row['provider'],row.get('provider_match_id')))
        candidates.extend((p,row.get(p+'_match_id')) for p in PROVIDERS)
        match,conflict,alias=collect('match',candidates)
        if alias:methods['match_entity_id']='registry_alias'
        if conflict:issues.append('conflicting_match_ids')
        result['match_entity_id']=match
        fixture=self.fixtures.get(match);meta=self.metadata.get(match,{})
        pair=(text(fixture.get('home_team_entity_id')),text(fixture.get('away_team_entity_id'))) if fixture else ('','')
        if pair[0] and pair[0]==pair[1]:issues.append('fixture_has_same_team_twice');pair=('','')
        if table=='fixtures':specs=[('home_team_entity_id','home_team_id','home'),('away_team_entity_id','away_team_id','away')]
        else:specs=[('team_entity_id','team_id',''),('opponent_entity_id','opponent_id','')]
        blocked=set()
        for column,native,side in specs:
            candidates=[(baseline,row.get(native))]
            if column=='team_entity_id' and row.get('provider') in PROVIDERS:
                candidates.append((row['provider'],row.get('provider_team_id')))
            if side:candidates.extend((p,row.get(p+'_'+side+'_team_id')) for p in PROVIDERS)
            entity,bad,alias=collect('team',candidates)
            if alias:methods[column]='registry_alias'
            if bad:blocked.add(column);issues.append('conflicting_'+column)
            if entity and all(pair) and entity not in pair:
                blocked.add(column);issues.append(column+'_outside_fixture');entity=''
            if side and entity and pair[0 if side=='home' else 1] and entity!=pair[0 if side=='home' else 1]:
                blocked.add(column);issues.append(column+'_wrong_side');entity=''
            result[column]=entity
        if fixture and all(pair):
            for column,_,side in specs:
                if result[column] or column in blocked:continue
                if side:
                    result[column]=pair[0 if side=='home' else 1];methods[column]='fixture_side'
            if table!='fixtures':
                # A provider's explicit home/away side is match-scoped evidence.
                side=text(row.get('side')).lower()
                target=pair[0 if side=='home' else 1] if side in ('home','away') else ''
                names={}
                for i,side in enumerate(('home','away')):
                    key=name_key(fixture.get(side+'_team'))
                    if key:names[key]=pair[i] if key not in names or names[key]==pair[i] else ''
                for i,side in enumerate(('home','away')):
                    for field in ('source_'+side+'_team',*(p+'_'+side+'_team' for p in PROVIDERS)):
                        key=name_key(meta.get(field))
                        if key:
                            if key in names and names[key]!=pair[i]:names[key]=''
                            else:names[key]=pair[i]
                if target and result['team_entity_id'] and target!=result['team_entity_id']:
                    blocked.add('team_entity_id');issues.append('team_side_disagrees');result['team_entity_id']=''
                if not target:target=names.get(name_key(row.get('team')),'')
                if target and not result['team_entity_id'] and 'team_entity_id' not in blocked:
                    result['team_entity_id']=target;methods['team_entity_id']='fixture_side_or_name'
                opponent=names.get(name_key(row.get('opponent')),'')
                if opponent and not result['opponent_entity_id'] and 'opponent_entity_id' not in blocked:
                    result['opponent_entity_id']=opponent;methods['opponent_entity_id']='fixture_opponent_name'
                for column,other in (('team_entity_id','opponent_entity_id'),('opponent_entity_id','team_entity_id')):
                    if not result[column] and column not in blocked and result[other] in pair:
                        result[column]=pair[1] if result[other]==pair[0] else pair[0];methods[column]='fixture_opponent'
                team,opponent=result['team_entity_id'],result['opponent_entity_id']
                if team and opponent and team==opponent:
                    issues.append('team_equals_opponent');result['team_entity_id']='';result['opponent_entity_id']=''
        if table=='player_match_stats' or 'player_id' in row:
            player,bad,alias=collect('player',[(baseline,row.get('player_id'))])
            if alias:methods['player_entity_id']='registry_alias'
            if bad:issues.append('conflicting_player_ids')
            result['player_entity_id']=player
        result['identity_resolution_status']='conflict' if issues else 'unresolved' if any(not v for v in result.values()) else 'resolved'
        result['identity_resolution_issues']=';'.join(issues)
        return result,methods


def annotate(dataset,source_root=None,*,report=print):
    """Add join-ready IDs to CSVs atomically; unchanged files retain their stamps."""
    from concordance.publication import folder,manifest_path
    dataset=Path(dataset);base=folder(dataset);catalog=digest(manifest_path(base));producer=digest(Path(__file__))
    previous_path=dataset/'id-resolution.json'
    try:previous=json.loads(previous_path.read_text())
    except (OSError,ValueError):previous={}
    cache={row['path']:row for row in previous.get('tables',[])}
    same=previous.get('concordance_digest')==catalog and previous.get('producer_digest')==producer
    resolver=None;tables=[]
    paths=sorted(p for p in (dataset/'raw_data').glob('*/*/*.csv') if p.stem in ('fixtures','player_match_stats','team_match_stats'))
    for index,path in enumerate(paths,1):
        relative=path.relative_to(dataset).as_posix();sha=digest(path)
        if resolver is None:resolver=Resolver(dataset)
        namespace=resolver.namespace(path,source_root)
        saved=cache.get(relative)
        if same and saved and saved['sha256']==sha and saved['id_namespace']==namespace:
            report(f'Reusing canonical IDs [{index}/{len(paths)}]: {relative}',flush=True)
            tables.append(saved);continue
        report(f'Resolving canonical IDs [{index}/{len(paths)}]: {relative}',flush=True)
        counts=Counter();methods=Counter();missing=Counter();samples=[];changed=False;rows=0
        temporary=path.with_name(path.name+'.ids.tmp')
        try:
            with path.open(encoding='utf-8-sig',newline='') as incoming,temporary.open('w',encoding='utf-8',newline='') as outgoing:
                reader=csv.DictReader(incoming)
                if not reader.fieldnames or 'match_id' not in reader.fieldnames:raise ValueError('Missing match_id: '+relative)
                fields=['match_entity_id',*(['home_team_entity_id','away_team_entity_id'] if path.stem=='fixtures' else ['team_entity_id','opponent_entity_id'])]
                if path.stem=='player_match_stats' or 'player_id' in reader.fieldnames:fields.append('player_entity_id')
                fields+=['identity_resolution_status','identity_resolution_issues']
                header=[*reader.fieldnames,*(c for c in fields if c not in reader.fieldnames)]
                writer=csv.DictWriter(outgoing,fieldnames=header);writer.writeheader()
                changed=header!=reader.fieldnames
                for row in reader:
                    if None in row or any(v is None for v in row.values()):raise ValueError('Malformed season CSV: '+relative)
                    resolved,recovered=resolver.row(row,path.stem,namespace);rows+=1
                    changed|=any(row.get(k)!=v for k,v in resolved.items())
                    counts[resolved['identity_resolution_status']]+=1;methods.update(recovered.values())
                    missing.update(k for k,v in resolved.items() if k.endswith('_entity_id') and not v)
                    if resolved['identity_resolution_status']!='resolved' and len(samples)<20:
                        samples.append({'row':rows,**{key:row.get(key,'') for key in ('match_id','team_id','opponent_id','player_id','provider','provider_team_id')},**resolved})
                    writer.writerow({**row,**resolved})
                outgoing.flush();os.fsync(outgoing.fileno())
            if changed:temporary.replace(path)
            else:temporary.unlink()
        finally:temporary.unlink(missing_ok=True)
        tables.append({'path':relative,'id_namespace':namespace,'rows':rows,'sha256':digest(path),
                       'status_counts':dict(counts),'recovery_methods':dict(methods),'missing_ids':dict(missing),'examples':samples})
    counts=Counter();missing=Counter();methods=Counter()
    for item in tables:counts.update(item['status_counts']);missing.update(item['missing_ids']);methods.update(item['recovery_methods'])
    value={'format':FORMAT,'producer_digest':producer,'concordance_digest':catalog,'tables':tables,
           'summary':{'rows':sum(r['rows'] for r in tables),'status_counts':dict(counts),'missing_ids':dict(missing),'recovery_methods':dict(methods)}}
    if value!=previous:atomic_json(previous_path,value,indent=2)
    return value

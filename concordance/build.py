"""Build six identity tables from fixture history and observed player statistics."""
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime,timezone,timedelta
import csv
import hashlib
import json
import re
from pathlib import Path
import tempfile

import pandas as pd
from filelock import FileLock

from collection.files import atomic_csv
from providers.reep import PROVIDERS,index
from providers.teamProfiles import canonical_team_id
from providers.teamScope import clean_reference,keep_reference
from providers.teamHistory import history_frame
from storage.pandasStore import atomic_json,atomic_replace
from .registry import Registry
from .sources import observations,source_signature,read_csv

FORMAT='soccer-concordance-bundle-v2'
ID_COLUMNS=['entity_id','reep_id','alex_id','id_source',*[p+'_id' for p in PROVIDERS],
            'identity_status','safe_for_cross_provider_joins']
TABLES=('full/fixtures.csv','full/entities.csv','full/teams.csv',
        'scraped/matches.csv','scraped/teams.csv','scraped/players.csv')


def invalidate_bundle(dataset,reason):
    path=Path(dataset)/'concordance/bundle.json'
    if not path.exists():return False
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('format')!=FORMAT:return False
    manifest.update(ready_for_R=False,invalidation_reason=reason,
                    invalidated_at=datetime.now(timezone.utc).isoformat())
    atomic_json(path,manifest,indent=2)
    return True


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):value.update(block)
    return value.hexdigest()


def table(path):
    return list(read_csv(path)) if Path(path).is_file() else []


def native(row,suffix='_id'):
    return {p+'_id':str(row.get(p+suffix) or '') for p in PROVIDERS}


def anchor(row):return next(((p,str(row[p+'_id'])) for p in PROVIDERS if row.get(p+'_id')),None)


def explicit_seeds(rows):
    """Observed multi-provider checkpoint links can enrich raw-only profiles."""
    result=[]
    for row in rows:
        ids=row.get('_native',{})
        if any(len(v)>1 for v in ids.values()):continue
        result.append({**{p+'_id':str(ids.get(p+'_id',[''])[0]) if ids.get(p+'_id') else '' for p in PROVIDERS},
                       'identity_status':row.get('identity_status','')})
    return result


class Pool:
    def __init__(self,kind,bridges,registry,seeds=()):
        self.kind,self.bridges,self.registry=kind,bridges,registry
        self.rows={};self.seeds=defaultdict(list)
        for seed in seeds:
            for p in PROVIDERS:
                if seed.get(p+'_id'):self.seeds[p,str(seed[p+'_id'])].append(seed)

    def add(self,values):
        values=dict(values);given=values.pop('_native',None)
        candidates={p+'_id':set(map(str,(given or {}).get(p+'_id',[]))) if given is not None
                    else {str(values[p+'_id'])} if values.get(p+'_id') else set() for p in PROVIDERS}
        aliases={k:set(v) for k,v in candidates.items()}
        for k,v in values.pop('_observed_native',{}).items():aliases.setdefault(k,set()).update(map(str,v))
        if values.get('provider_aliases'):
            for k,v in json.loads(values['provider_aliases']).items():aliases.setdefault(k,set()).update(map(str,v))
        if self.kind=='team':
            candidates={k:{canonical_team_id(k[:-3],v) for v in vals} for k,vals in candidates.items()}
        clean={k:next(iter(v)) if len(v)==1 else '' for k,v in candidates.items()}
        if self.kind=='team':
            previous=dict(clean)
            clean=clean_reference({**values,**clean})
            if not keep_reference(clean):return None
            for p in PROVIDERS:
                if previous.get(p+'_id') and not clean.get(p+'_id'):candidates[p+'_id']=set()
        # Existing catalogs enrich observed IDs; they do not establish scraping.
        found={id(s):s for p in PROVIDERS for v in candidates[p+'_id'] for s in self.seeds[p,v]}
        for seed in found.values():
            if any(clean.get(p+'_id') and seed.get(p+'_id') and clean[p+'_id']!=seed[p+'_id'] for p in PROVIDERS):continue
            for p in PROVIDERS:
                if seed.get(p+'_id'):candidates[p+'_id'].add(str(seed[p+'_id']))
            if seed.get('identity_status')=='conflict':values['identity_status']='conflict'
        clean={k:next(iter(v)) if len(v)==1 else '' for k,v in candidates.items()}
        resolved=self.bridges.resolve(self.kind,clean)
        if resolved.get('reep_id'):resolved['reep_id']=self.bridges.canonical_id(resolved['reep_id'])
        # Preserve a published ID already present in a received export if the
        # compact installed index has no bridge for it. Redirects are followed.
        known=values.get('reep_id','')
        if known and not resolved.get('reep_id') and resolved['identity_status']!='conflict':
            resolved['reep_id']=self.bridges.canonical_id(known)
        for p in PROVIDERS:
            if resolved.get(p+'_id'):candidates[p+'_id'].add(resolved[p+'_id'])
        key=anchor(resolved)
        if key is None:raise ValueError('Reference has no provider ID')
        item=self.rows.setdefault(key,{'sets':{p+'_id':set() for p in PROVIDERS},'aliases':defaultdict(set),'values':{},'context':defaultdict(set),'conflict':False,'reep':set(),'statuses':set()})
        for p in PROVIDERS:
            item['sets'][p+'_id'].update(candidates[p+'_id'])
            item['aliases'][p+'_id'].update(aliases[p+'_id']|candidates[p+'_id'])
        item['conflict'] |= (values.get('identity_status')=='conflict' or resolved['identity_status']=='conflict' or any(len(v)>1 for v in candidates.values()))
        if resolved.get('reep_id'):item['reep'].add(resolved['reep_id'])
        item['statuses'].add(resolved['identity_status'])
        for k,v in values.items():
            if k in ('competition','season','team_name') and v:item['context'][k].add(str(v))
            if k in ('team_name','player_name','date','home_team_name','away_team_name') and v:item['values'].setdefault(k,v)
        return key

    def finish(self):
        from .reconcile import rejected_links,annotate_names
        observations=[]
        for key,item in self.rows.items():
            native={p+'_id':next(iter(item['sets'][p+'_id'])) if len(item['sets'][p+'_id'])==1 else '' for p in PROVIDERS}
            native[key[0]+'_id']=key[1];observations.append(native)
            for p in PROVIDERS:
                if p!=key[0] and len(item['sets'][p+'_id'])>1:
                    observations.extend({**native,p+'_id':v} for v in item['sets'][p+'_id'])
        reject,verified=rejected_links(self.kind,observations,self.bridges)
        for _,p,v in reject:self.registry.withhold(self.kind,p,v,'ambiguous_provider_link')
        result=[]
        for key,item in sorted(self.rows.items()):
            peers={p+'_id':next(iter(item['sets'][p+'_id'])) if len(item['sets'][p+'_id'])==1 else '' for p in PROVIDERS}
            # Retain the primary observed ID even when peer profiles conflict.
            peers[key[0]+'_id']=key[1]
            conflict=item['conflict'] or len(item['reep'])>1 or any(len(v)>1 for v in item['sets'].values())
            published=next(iter(item['reep'])) if len(item['reep'])==1 and not conflict else ''
            baseline_links=self.bridges.ids(self.kind,*key)
            if not published and len(baseline_links)==1:published=self.bridges.canonical_id(next(iter(baseline_links)))
            assigned=self.registry.reconcile(self.kind,peers,published,conflict=conflict,reject=reject,verified=verified)
            for p in assigned['rejected_ids']:peers[p+'_id']=''
            conflict=conflict or not assigned['safe_for_cross_provider_joins']
            assigned={k:v for k,v in assigned.items() if k!='rejected_ids'}
            row={**item['values'],**peers,**assigned,
                 'identity_status':'conflict' if conflict else ('linked' if item['statuses']=={'linked'} else 'partly_verified') if assigned['reep_id'] else 'local',
                 'safe_for_cross_provider_joins':not conflict,
                 'provider_aliases':json.dumps({p:sorted(v) for p,v in item['aliases'].items()},sort_keys=True),
                 'competitions':json.dumps(sorted(item['context']['competition'])),
                 'seasons':json.dumps(sorted(item['context']['season'])),
                 'teams':json.dumps(sorted(item['context']['team_name']))}
            if self.kind=='match':
                row['competition']=next(iter(item['context']['competition'])) if len(item['context']['competition'])==1 else ''
                row['season']=next(iter(item['context']['season'])) if len(item['context']['season'])==1 else ''
            result.append(row)
        # A raw-only profile and its checkpoint can reach the same proven
        # canonical identity through different anchors. Coalesce compatible
        # references; never choose between contradictory native provider IDs.
        unique={}
        for row in result:
            old=unique.get(row['entity_id'])
            if old is None:unique[row['entity_id']]=row;continue
            for p in PROVIDERS:
                k=p+'_id'
                if old.get(k) and row.get(k) and old[k]!=row[k]:
                    candidates=(old[k],row[k])
                    links=[{self.bridges.canonical_id(x) for x in self.bridges.ids(self.kind,p,v)} for v in candidates]
                    if all(values=={row['entity_id']} for values in links):
                        row[k]=old[k]
                        continue
                    if p!='sofascore':old[k]=row[k]=''
                    old['safe_for_cross_provider_joins']=row['safe_for_cross_provider_joins']=False
                    old['identity_status']=row['identity_status']='conflict'
                    for v in candidates:self.registry.note(self.kind,p,v,'multiple_native_ids_for_entity',entity_id=row['entity_id'])
                if not old.get(k):old[k]=row.get(k,'')
            for k in ('competitions','seasons','teams'):
                old[k]=json.dumps(sorted(set(json.loads(old[k]))|set(json.loads(row[k]))))
            a,b=json.loads(old['provider_aliases']),json.loads(row['provider_aliases'])
            old['provider_aliases']=json.dumps({k:sorted(set(a.get(k,[]))|set(b.get(k,[]))) for k in set(a)|set(b)},sort_keys=True)
            old['safe_for_cross_provider_joins'] &= row['safe_for_cross_provider_joins']
            if row['identity_status']=='conflict':old['identity_status']='conflict'
            elif row['identity_status']=='partly_verified' and old['identity_status']!='conflict':old['identity_status']='partly_verified'
            for k,v in row.items():
                if (k not in old or old[k] is None or old[k]=='') and v:old[k]=v
        rows=list(unique.values())
        if self.kind=='player':annotate_names(rows)
        return rows


def verified_integration(root,report):
    if report is None:return False
    value=json.loads(Path(report).read_text(encoding='utf-8'))
    if value.get('format')!='soccer-integration-audit-v1' or value.get('state')!='ready':
        raise ValueError('MSI integration audit is not ready')
    if Path(value['data_root']).resolve()!=Path(root).resolve():raise ValueError('Integration audit belongs to another data root')
    if datetime.now(timezone.utc)-datetime.fromisoformat(value['checked_at'])>timedelta(hours=24):
        raise ValueError('Integration audit is stale; rerun it before rebuilding')
    from tools.audit_integration import inspect_queues
    _,problems=inspect_queues(root,value.get('ensemble_root',Path(root).parent/'ensemble_data'))
    if problems:raise ValueError('MSI integration changed: '+'; '.join(problems))
    return True


def validate_bundle(folder):
    """Check all canonical keys, provider anchors, foreign keys and checksums."""
    folder=Path(folder);manifest=json.loads((folder/'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=FORMAT:raise ValueError('Unsupported concordance bundle')
    for relative,sha in manifest.get('auxiliary_checksums',{}).items():
        if relative not in ('id_redirects.csv','quarantine.csv','alex_registry.json','registry_conflicts.csv') or digest(folder/relative)!=sha:
            raise ValueError('Concordance sidecar checksum mismatch')
    frames={}
    for relative in TABLES:
        path=folder/relative
        if digest(path)!=manifest['files'][relative]['sha256']:raise ValueError('Concordance checksum mismatch: '+relative)
        rows=table(path);frames[relative]=rows
        keys=[]
        for row in rows:
            endpoints=('from_','to_') if relative=='full/entities.csv' else ('',)
            for prefix in endpoints:
                entity=row.get(prefix+'entity_id','')
                if not entity or not any(row.get(prefix+p+'_id') for p in PROVIDERS):
                    raise ValueError('Missing canonical/provider anchor: '+relative)
                if row.get(prefix+'id_source')=='alex' and not entity.startswith(('am','at','ap')):
                    raise ValueError('Invalid Alex ID namespace')
            keys.append(row['relationship_id'] if relative=='full/entities.csv' else row['entity_id'])
        if len(keys)!=len(set(keys)):raise ValueError('Duplicate canonical key: '+relative)
    registry=Registry(folder/'alex_registry.json')
    checked=0
    for relative,kind in (('full/fixtures.csv','match'),('full/teams.csv','team'),
                          ('scraped/matches.csv','match'),('scraped/teams.csv','team'),('scraped/players.csv','player')):
        checked+=registry.validate_rows(kind,frames[relative])
    for prefix in ('from_','to_'):checked+=registry.validate_rows('team',frames['full/entities.csv'],prefix=prefix)
    for side in ('home','away'):
        references=[{'entity_id':r[side+'_entity_id'],
            'safe_for_cross_provider_joins':r.get(side+'_safe_for_cross_provider_joins',False),
            **{p+'_id':r.get(p+'_'+side+'_team_id','') for p in PROVIDERS}} for r in frames['full/fixtures.csv']]
        checked+=registry.validate_rows('team',references)
    team_ids={r['entity_id'] for r in frames['full/teams.csv']}
    for row in frames['full/fixtures.csv']:
        if row['home_entity_id'] not in team_ids or row['away_entity_id'] not in team_ids:
            raise ValueError('Fixture references a missing full team')
    known_aliases={p:set() for p in PROVIDERS}
    for row in frames['full/teams.csv']:
        aliases=json.loads(row.get('provider_aliases') or '{}')
        for p in PROVIDERS:
            known_aliases[p].update(aliases.get(p+'_id',[]))
            if row.get(p+'_id'):known_aliases[p].add(row[p+'_id'])
    for row in frames['full/fixtures.csv']:
        for p in PROVIDERS:
            for side in ('home','away'):
                value=row.get(p+'_'+side+'_team_id')
                if value and value not in known_aliases[p]:raise ValueError('Fixture native team ID is absent from the full team aliases')
    return {'rows':{k:len(v) for k,v in frames.items()},'id_requirements_met':True,
            'integration_confirmed':manifest['integration_confirmed'],'ready_for_R':manifest['ready_for_R'],
            'provider_id_alignment':{'checked_provider_keys':checked,'inconsistent_keys':0}}


def rebuild(root,dataset,*,output=None,integration_report=None,allow_unconfirmed=False,report=print):
    root,dataset=Path(root).resolve(),Path(dataset).resolve()
    if root==dataset or root in dataset.parents or dataset in root.parents:
        raise ValueError('Raw data and dataset roots must be separate directories')
    output=Path(output).resolve() if output else dataset/'concordance'
    confirmed=verified_integration(root,integration_report)
    if not confirmed and not allow_unconfirmed:raise ValueError('Run tools.audit_integration on MSI first; a ready integration report is required')
    output.mkdir(parents=True,exist_ok=True)
    state_root=root if confirmed else output/'.preview-state'
    (state_root/'_system/identity').mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(FileLock(str(state_root/'_system/identity/concordance-rebuild.lock'),timeout=0))
        registry_path=state_root/'_system/identity/alex_ids.json';registry_path.parent.mkdir(parents=True,exist_ok=True)
        stack.enter_context(FileLock(str(registry_path)+'.lock',timeout=0))
        before=source_signature(root,dataset)
        seed=output/'alex_registry.json'
        if not seed.exists():seed=root/'_system/identity/alex_ids.json'
        if not seed.exists():seed=dataset/'fixture_concordance/alex_registry.json'
        from .sync import load as load_registry
        registry=load_registry(root,path=registry_path,seeds=[seed]);bridges=index(root)
        registry.value['redirects'].update(bridges.metadata.get('redirects',{}))
        fixtures=table(dataset/'fixtures.csv')
        if not fixtures:raise ValueError('The full 2010+ fixture export is required')
        raw=observations(root,dataset,report=report)
        # Recover observed fixtures absent from an older global publication.
        present={r['sofascore_match_id'] for r in fixtures}
        excluded_pairs={(r['provider'],r['match_id']) for r in table(dataset/'fixture_concordance/attempts.csv') if r.get('canonical_match_id')}
        excluded={mid for p,mid in excluded_pairs if p=='sofascore'}
        raw['observed']-=excluded_pairs
        raw['match']=[r for r in raw['match'] if not any((p,v) in excluded_pairs for p in PROVIDERS for v in r['_native'].get(p+'_id',[]))]
        for path in sorted(dataset.glob('*/*/fixtures.csv')):
            if not re.fullmatch(r'\d{4}-\d{4}',path.parent.name):continue
            for row in read_csv(path):
                mid=row.get('match_id','')
                if ('sofascore',mid) in raw['observed'] and mid not in present and mid not in excluded:
                    fixtures.append({**row,'sofascore_match_id':mid,'fixture_source':'scraped_season_export'})
                    present.add(mid)
        # Deferred flat exports must not hide a scraped checkpoint's fixture.
        from dataset_builder import fixtures_from_events,_identifier
        for path in sorted(root.glob('*/*/schedule.json')):
            config_path=path.parent/'config.json'
            if not config_path.is_file():continue
            cfg=json.loads(config_path.read_text(encoding='utf-8'))
            if cfg.get('id_namespace',cfg.get('baseline_provider','sofascore'))!='sofascore':continue
            for event in json.loads(path.read_text(encoding='utf-8')).get('events',[]):
                mid=str(event['id'])
                if ('sofascore',mid) not in raw['observed'] or mid in present or mid in excluded:continue
                frame=fixtures_from_events([event],cfg['competition'],cfg['season'],include_qualifying=True)
                row={k:_identifier(v) for k,v in frame.iloc[0].to_dict().items()}
                row['sofascore_match_id']=mid;row['fixture_source']='scraped_checkpoint_schedule'
                fixtures.append(row);present.add(mid)
        match_seeds=[{**r,**{p+'_id':r.get(p+'_match_id','') for p in PROVIDERS},
                      'identity_status':r.get('match_identity_status',''),'reep_id':r.get('reep_match_id','')} for r in fixtures]
        team_seeds=table(dataset/'fixture_concordance/team_ids.csv')
        all_teams=Pool('team',bridges,registry,team_seeds)
        for row in fixtures:
            for side in ('home','away'):
                peers={p+'_id':row.get(p+'_'+side+'_team_id','') for p in PROVIDERS}
                peers['sofascore_id']=row.get(side+'_team_id') or peers['sofascore_id']
                all_teams.add({**peers,'reep_id':row.get('reep_'+side+'_team_id',''),
                    '_observed_native':{p+'_id':[row[p+'_'+side+'_team_id']] for p in PROVIDERS if row.get(p+'_'+side+'_team_id')},
                    'team_name':row[side+'_team'],'identity_status':row.get(side+'_team_identity_status',''),
                    'competition':row['competition'],'season':row['season']})
        full_teams=all_teams.finish()
        team_lookup={(p,r[p+'_id']):r for r in full_teams for p in PROVIDERS if r.get(p+'_id')}
        for row in full_teams:
            for p,values in json.loads(row['provider_aliases']).items():
                provider=p.removesuffix('_id')
                for native in values:
                    if registry.lookup('team',provider,native)==row['entity_id']:team_lookup[provider,native]=row
        match_seeds.extend(explicit_seeds(raw['match']))
        all_matches=Pool('match',bridges,registry,match_seeds)
        for row in match_seeds:all_matches.add(row)
        for row in raw['match']:all_matches.add(row)
        match_references=all_matches.finish()
        match_lookup={(p,r[p+'_id']):r for r in match_references for p in PROVIDERS if r.get(p+'_id')}
        full_fixtures=[]
        for row in fixtures:
            match=match_lookup['sofascore',row['sofascore_match_id']]
            result={**row,**{k:match.get(k,'') for k in ID_COLUMNS}}
            for p in PROVIDERS:
                if not result.get(p+'_match_id'):result[p+'_match_id']=match.get(p+'_id','')
            for side in ('home','away'):
                tid=canonical_team_id('sofascore',row.get(side+'_team_id') or row.get('sofascore_'+side+'_team_id'))
                result[side+'_entity_id']=team_lookup['sofascore',tid]['entity_id']
            full_fixtures.append(result)
        scraped_matches=Pool('match',bridges,registry,match_seeds)
        for p,mid in sorted(raw['observed']):
            if p=='sofascore' and mid in excluded:continue
            seed=match_lookup.get((p,mid),{})
            scraped_matches.add({**seed,p+'_id':mid})
        for row in raw['match']:
            if any((p,v) in raw['observed'] for p in PROVIDERS for v in row['_native'].get(p+'_id',[])):
                scraped_matches.add(row)
        scraped_match_rows=scraped_matches.finish()
        scraped_ids={r['entity_id'] for r in scraped_match_rows}
        observed_teams={row[side+'_entity_id'] for row in full_fixtures if row['entity_id'] in scraped_ids for side in ('home','away')}
        scraped_teams=Pool('team',bridges,registry,full_teams)
        for row in full_teams:
            if row['entity_id'] in observed_teams:scraped_teams.add(row)
        for row in raw['team']:
            scraped_teams.add(row)
        players=Pool('player',bridges,registry,[*table(dataset/'concordance/player_ids.csv'),*explicit_seeds(raw['player'])])
        for row in raw['player']:players.add(row)
        player_rows=players.finish()
        entities=[];quarantine=[]
        for row in history_frame(root).to_dict('records'):
            if any(not any(str(row.get(side+'_'+p+'_id') or '') for p in PROVIDERS) for side in ('from','to')):
                quarantine.append({'table':'full/entities.csv','reason':'Historical endpoint has no provider ID',
                                   'record':json.dumps(row,ensure_ascii=False)})
                continue
            for side in ('from','to'):
                peers={p+'_id':str(row.get(side+'_'+p+'_id') or '') for p in PROVIDERS}
                # A researched lineage edge is not a provider identity bridge.
                assigned=registry.reconcile('team',peers,str(row.get(side+'_reep_id') or ''),conflict=True)
                assigned.pop('rejected_ids',None)
                row[side+'_safe_for_cross_provider_joins']=False
                for field,value in assigned.items():row[side+'_'+field]=value
            entities.append(row)
        outputs=dict(zip(TABLES,[full_fixtures,entities,full_teams,scraped_match_rows,scraped_teams.finish(),player_rows]))
        # Later observations may upgrade an earlier alias. Publish every
        # reference using the final registry, including fixture foreign keys.
        for rows in outputs.values():
            for row in rows:
                for field in ('entity_id','home_entity_id','away_entity_id','from_entity_id','to_entity_id'):
                    if row.get(field):row[field]=registry.canonical(row[field])
                for prefix in ('','from_','to_'):
                    if row.get(prefix+'alex_id'):
                        row[prefix+'alex_id']=registry.follow(row[prefix+'alex_id'])
                        entity=registry.canonical(row[prefix+'alex_id'])
                        row[prefix+'reep_id']=entity if entity!=row[prefix+'alex_id'] else ''
                        row[prefix+'id_source']='reep' if row[prefix+'reep_id'] else 'alex'
        if source_signature(root,dataset)!=before:raise ValueError('Collection inputs changed during rebuild; previous bundle preserved; pause writes and retry')
        if confirmed:verified_integration(root,integration_report)
        with tempfile.TemporaryDirectory(prefix='.concordance-',dir=output.parent) as temp:
            stage=Path(temp);files={}
            for relative,rows in outputs.items():
                columns=list(dict.fromkeys([*ID_COLUMNS,*[k for row in rows for k in row]])) if relative!='full/entities.csv' else list(dict.fromkeys([k for row in rows for k in row]))
                if not columns:columns=['relationship_id','from_entity_id','to_entity_id']
                path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True)
                atomic_csv(pd.DataFrame(rows,columns=columns).fillna(''),path)
                files[relative]={'rows':len(rows),'sha256':digest(path),'alex_ids':sum(r.get('id_source')=='alex' for r in rows)}
            redirects=registry.redirects();atomic_csv(pd.DataFrame(redirects,columns=['from_id','to_id']),stage/'id_redirects.csv')
            atomic_csv(pd.DataFrame(quarantine,columns=['table','reason','record']),stage/'quarantine.csv')
            atomic_json(stage/'alex_registry.json',registry.value,sort_keys=True)
            from fixtures.sharedIds import ISSUE_COLUMNS
            atomic_csv(pd.DataFrame(registry.issues,columns=ISSUE_COLUMNS).drop_duplicates(),stage/'registry_conflicts.csv')
            from .sync import version as registry_version,installed_version
            manifest={'registry_version':registry_version(registry.value),'main_registry_version':installed_version(root),'format':FORMAT,'built_at':datetime.now(timezone.utc).isoformat(),'integration_confirmed':confirmed,
                      'ready_for_R':confirmed,'id_requirements_met':True,'source_signature':before,'files':files,
                      'sources':raw['sources'],'scraped_definition':'Matches with saved player-stat rows or validated checkpoint performance values',
                      'unmatched_provider_ids_are_blank':True,'canonical_id_column':'entity_id',
                      'published_reep_ids_are_not_generated':True,'quarantined_relationships':len(quarantine),
                      'reep_release':bridges.metadata.get('release',''),
                      'auxiliary_checksums':{name:digest(stage/name) for name in ('id_redirects.csv','quarantine.csv','alex_registry.json','registry_conflicts.csv')}}
            atomic_json(stage/'bundle.json',manifest,indent=2)
            validate_bundle(stage)
            # Registry first: interrupted exports must never forget minted IDs.
            registry.save()
            for relative in (*TABLES,'id_redirects.csv','quarantine.csv','alex_registry.json','registry_conflicts.csv','bundle.json'):
                target=output/relative;target.parent.mkdir(parents=True,exist_ok=True)
                atomic_replace(stage/relative,target)
        return manifest

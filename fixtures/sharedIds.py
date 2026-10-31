"""Canonical fixture/reference IDs with explicit provider-link quarantine."""
from pathlib import Path
import json

import pandas as pd
from filelock import FileLock

from collection.files import atomic_csv
from concordance.sync import load as load_registry,version
from concordance.reconcile import rejected_links,annotate_names
from providers.reep import PROVIDERS,index
from providers.teamProfiles import canonical_team_id
from storage.pandasStore import atomic_json

FIXTURE_KEYS=('entity_id','alex_match_id','id_source','safe_for_cross_provider_joins',
              'home_entity_id','alex_home_team_id','home_id_source','home_safe_for_cross_provider_joins',
              'away_entity_id','alex_away_team_id','away_id_source','away_safe_for_cross_provider_joins')
REFERENCE_KEYS=('entity_id','alex_id','id_source','safe_for_cross_provider_joins','name_aliases')
ISSUE_COLUMNS=('kind','provider','native_id','reason','candidate_id','entity_id')


def assign_ids(root,fixtures,matches=(),teams=(),players=(),*,folder=None,save=True):
    """Use the shared registry; native IDs withheld from joins remain in audit."""
    root=Path(root);path=root/'_system/identity/alex_ids.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    folder=Path(folder) if folder is not None else None
    bridges=index(root)
    with FileLock(str(path)+'.lock',timeout=25):
        seeds=list(dict.fromkeys([folder/'alex_registry.json',folder.parent/'concordance/alex_registry.json',
            folder.parent/'fixture_concordance/alex_registry.json'])) if folder else []
        registry=load_registry(root,seeds=seeds)
        registry.value['redirects'].update(bridges.metadata.get('redirects',{}))
        specs=[]
        def add(row,kind,suffix,prefix='',reference=False):
            native={p+'_id':str(row.get(p+suffix) or '') for p in PROVIDERS}
            if kind=='team':native={k:canonical_team_id(k[:-3],v) if v else '' for k,v in native.items()}
            anchor=next(((p,native[p+'_id']) for p in PROVIDERS if native[p+'_id']),None)
            if anchor is None:raise ValueError('Shared IDs require at least one real provider ID')
            status=row.get('identity_status' if reference else prefix+'team_identity_status' if prefix else 'match_identity_status')
            published=bridges.canonical_id(str(row.get('reep'+suffix) or '')) if status!='conflict' else ''
            if not published and status!='conflict':
                candidates=bridges.ids(kind,*anchor)
                if len(candidates)==1:published=bridges.canonical_id(next(iter(candidates)))
            specs.append(dict(row=row,kind=kind,native=native,published=published,prefix=prefix,
                reference=reference,status=status,suffix=suffix,anchor=anchor))
        for row in fixtures:
            add(row,'match','_match_id')
            for side in ('home','away'):add(row,'team','_'+side+'_team_id',side+'_')
        for kind,rows in (('match',matches),('team',teams),('player',players)):
            for row in rows:add(row,kind,'_id',reference=True)
        rejects={};verified={}
        for kind in ('match','team','player'):
            observations=[]
            for s in specs:
                if s['kind']!=kind:continue
                observations.append(s['native'])
                candidates=json.loads(s['row'].get('provider_id_candidates') or '{}')
                for p in PROVIDERS:
                    if p!=s['anchor'][0] and len(candidates.get(p+'_id',[]))>1:
                        observations.extend({**s['native'],p+'_id':v} for v in candidates[p+'_id'])
            rejects[kind],verified[kind]=rejected_links(kind,observations,bridges)
            for _,provider,native in rejects[kind]:
                registry.withhold(kind,provider,native,'ambiguous_provider_link')
                registry.note(kind,provider,native,'ambiguous_provider_link')
        # Register published baseline anchors first, independently of peer IDs.
        for s in specs:
            if s['published']:
                registry.reconcile(s['kind'],{s['anchor'][0]+'_id':s['anchor'][1]},s['published'])
        excluded=0
        for s in specs:
            row,kind,native,published,prefix=s['row'],s['kind'],s['native'],s['published'],s['prefix']
            result=registry.reconcile(kind,native,published,conflict=s['status']=='conflict',
                reject=rejects[kind],verified=verified[kind])
            candidates=json.loads(row.get('provider_id_candidates') or '{}')
            if any(len(ids)>1 and any((kind,p.removesuffix('_id'),v) in rejects[kind] for v in ids) for p,ids in candidates.items()):
                result['safe_for_cross_provider_joins']=False
            s['assigned']=result
            safe=result['safe_for_cross_provider_joins'];excluded+=not safe
            for p in result['rejected_ids']:
                row[p+s['suffix']]=''
                if not prefix and not s['reference']:
                    row[p+'_fixture_check']='identity_conflict';row[p+'_match_method']=''
            row['reep'+s['suffix']]=result['reep_id']
            status_key='identity_status' if s['reference'] else prefix+'team_identity_status' if prefix else 'match_identity_status'
            if not row.get(status_key):row[status_key]='linked' if result['reep_id'] else 'local'
            if not safe:
                row['identity_status' if s['reference'] else prefix+'team_identity_status' if prefix else 'match_identity_status']='conflict'
        generated={k:set() for k in ('match','team','player')};checked={}
        for s in specs:
            row,kind,prefix=s['row'],s['kind'],s['prefix']
            result=registry.reconcile(kind,{s['anchor'][0]+'_id':s['anchor'][1]})
            safe=s['assigned']['safe_for_cross_provider_joins']
            # Later observations can upgrade an alias. Publish the final key.
            if s['reference']:
                row.update({k:result[k] for k in ('entity_id','alex_id','id_source')})
                row['safe_for_cross_provider_joins']=safe
            else:
                row[prefix+'entity_id']=result['entity_id']
                row['alex_'+('match_id' if not prefix else prefix+'team_id')]=result['alex_id']
                row[prefix+'id_source']=result['id_source'];row[prefix+'safe_for_cross_provider_joins']=safe
            row['reep'+s['suffix']]=result['reep_id']
            for p in PROVIDERS:
                native=str(row.get(p+s['suffix']) or '')
                if kind=='team':native=canonical_team_id(p,native)
                if not native or (not safe and p!=s['anchor'][0]):continue
                token=kind,p,native
                if registry.lookup(*token)!=result['entity_id'] or checked.setdefault(token,result['entity_id'])!=result['entity_id']:
                    raise ValueError('Provider ID resolves to inconsistent canonical IDs: '+str(token))
            if result['id_source']=='alex':generated[kind].add(result['entity_id'])
        annotate_names(players)
        if len({r['entity_id'] for r in fixtures})!=len(fixtures):raise ValueError('Duplicate canonical fixture key')
        issues=list({json.dumps(r,sort_keys=True):r for r in registry.issues}.values())
        if save:
            registry.save()
            if issues:atomic_json(root/'_system/identity/registry_conflicts.json',issues)
            if folder is not None:
                folder.mkdir(parents=True,exist_ok=True)
                atomic_json(folder/'alex_registry.json',registry.value,sort_keys=True)
                atomic_csv(pd.DataFrame(registry.redirects(),columns=['from_id','to_id']),folder/'id_redirects.csv')
                atomic_csv(pd.DataFrame(issues,columns=ISSUE_COLUMNS),folder/'registry_conflicts.csv')
        return {'canonical_id_column':'entity_id','all_canonical_ids_present':True,
                'generated_ids':{k:len(v) for k,v in generated.items()},'registry_version':version(registry.value),
                'registry_conflicts':len(issues),'published_reep_ids_are_not_generated':True,
                'provider_id_alignment':{'checked_provider_keys':len(checked),
                    'inconsistent_keys':0,'excluded_ambiguous_observations':excluded}}

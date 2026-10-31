"""Canonical fixture/team keys using published Reep IDs or stable Alex aliases."""
from pathlib import Path

import pandas as pd
from filelock import FileLock

from collection.files import atomic_csv
from concordance.registry import Registry
from providers.reep import PROVIDERS,index
from providers.teamProfiles import canonical_team_id
from storage.pandasStore import atomic_json

FIXTURE_KEYS=('entity_id','alex_match_id','id_source','safe_for_cross_provider_joins',
              'home_entity_id','alex_home_team_id','home_id_source','home_safe_for_cross_provider_joins',
              'away_entity_id','alex_away_team_id','away_id_source','away_safe_for_cross_provider_joins')
REFERENCE_KEYS=('entity_id','alex_id','id_source','safe_for_cross_provider_joins')


def assign_ids(root,fixtures,matches=(),teams=(),*,folder=None,save=True):
    """Bind baseline anchors only; disputed provider peers never become aliases."""
    root=Path(root);path=root/'_system/identity/alex_ids.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    folder=Path(folder) if folder is not None else None
    bridges=index(root)
    with FileLock(str(path)+'.lock',timeout=25):
        registry=Registry(path,seed=folder/'alex_registry.json' if folder else None)
        registry.value['redirects'].update(bridges.metadata.get('redirects',{}))
        specs=[]
        def add(row,kind,suffix,prefix='',reference=False):
            native={p+'_id':str(row.get(p+suffix) or '') for p in PROVIDERS}
            if kind=='team':
                native={k:canonical_team_id(k[:-3],v) if v else '' for k,v in native.items()}
            anchor=next(((p,native[p+'_id']) for p in PROVIDERS if native[p+'_id']),None)
            if anchor is None:raise ValueError('Shared fixture IDs require at least one real provider ID')
            status=row.get('identity_status' if reference else prefix+'team_identity_status' if prefix else 'match_identity_status')
            published=bridges.canonical_id(str(row.get('reep'+suffix) or '')) if status!='conflict' else ''
            specs.append((row,kind,{anchor[0]+'_id':anchor[1]},published,prefix,reference,status))
        for row in fixtures:
            add(row,'match','_match_id')
            for side in ('home','away'):add(row,'team','_'+side+'_team_id',side+'_')
        for kind,rows in (('match',matches),('team',teams)):
            for row in rows:add(row,kind,'_id',reference=True)
        # Register published anchors first so a historical blank for a known
        # team cannot get a different canonical key within this same export.
        for _,kind,native,published,_,_,_ in specs:
            if published:registry.assign(kind,native,published,conflict=True)
        generated={'match':set(),'team':set()}
        for row,kind,native,published,prefix,reference,status in specs:
            assigned=registry.assign(kind,native,published,conflict=True)
            if reference:
                row.update({k:assigned[k] for k in ('entity_id','alex_id','id_source')})
                row['safe_for_cross_provider_joins']=status!='conflict'
            else:
                row[prefix+'entity_id']=assigned['entity_id']
                row['alex_'+('match_id' if not prefix else prefix+'team_id')]=assigned['alex_id']
                row[prefix+'id_source']=assigned['id_source']
                row[prefix+'safe_for_cross_provider_joins']=status!='conflict'
            if assigned['id_source']=='alex':generated[kind].add(assigned['entity_id'])
        if len({r['entity_id'] for r in fixtures})!=len(fixtures):
            raise ValueError('Duplicate canonical fixture key')
        if save:
            registry.save()
            if folder is not None:
                folder.mkdir(parents=True,exist_ok=True)
                atomic_json(folder/'alex_registry.json',registry.value,sort_keys=True)
                atomic_csv(pd.DataFrame(registry.redirects(),columns=['from_id','to_id']),folder/'id_redirects.csv')
        return {'canonical_id_column':'entity_id','all_canonical_ids_present':True,
                'generated_ids':{k:len(v) for k,v in generated.items()},
                'published_reep_ids_are_not_generated':True}

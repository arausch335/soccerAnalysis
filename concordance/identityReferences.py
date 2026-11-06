"""Keep stale, unlinked catalogue references out of the usable ID tables."""
from collections import defaultdict
import json

import pandas as pd

from providers.reep import PROVIDERS


def accepted_aliases(registry,kind):
    result=defaultdict(lambda:defaultdict(set))
    if registry is None:return result
    for token,alias in registry.value['aliases'].items():
        namespace,provider,native=token.split('|',2)
        if namespace!=kind or token in registry.value.get('withheld',{}):continue
        if kind=='team':
            from .teamNamespace import valid_native_team_id
            if not valid_native_team_id(native):continue
        entity=registry.canonical(alias)
        if registry.lookup(kind,provider,native)==entity:result[entity][provider+'_id'].add(native)
    return result


def separate_unlinked(kind,ids,details,references,fixtures,entities,dataset):
    """Quarantine unused references; a used identity still needs a real alias.

    Never invent an ID or redirect a record to an alias's new owner. Scan the
    exported canonical columns only when an orphan actually needs checking.
    """
    orphaned={row['entity_id'] for row in ids if not any(row[p+'_id'] for p in PROVIDERS)}
    if not orphaned:return ids,details,[]
    by_entity=defaultdict(list)
    for row in references:
        if row.get('entity_id') in orphaned:by_entity[row['entity_id']].append(row)
    used=defaultdict(set)
    fixture_keys={'match':('entity_id',),'team':('home_entity_id','away_entity_id'),'player':()}[kind]
    for row in fixtures:
        for key in fixture_keys:
            if row.get(key) in orphaned:used[row[key]].add('fixtures')
    for row in entities:
        for key in ('from_entity_id','to_entity_id'):
            if row.get(key) in orphaned:used[row[key]].add('entities')
    columns={'match':('match_entity_id','entity_match_id'),
             'team':('team_entity_id','opponent_entity_id','entity_team_id','entity_opponent_team_id',
                     'home_team_entity_id','away_team_entity_id','entity_home_team_id','entity_away_team_id'),
             'player':('player_entity_id','entity_player_id')}[kind]
    from .layout import public_root
    dataset=public_root(dataset)
    for path in sorted((dataset/'raw_data').glob('*/*/*.csv')):
        for chunk in pd.read_csv(path,dtype=str,keep_default_na=False,chunksize=50_000,
                                 usecols=lambda name:name in columns,encoding='utf-8-sig'):
            for key in chunk:
                for entity in set(chunk[key]) & orphaned:used[entity].add(path.relative_to(dataset).as_posix())
    if used:
        examples=[]
        for entity,paths in sorted(used.items())[:10]:
            observed={provider:sorted({str(row.get(provider+'_id')) for row in by_entity[entity] if row.get(provider+'_id')})[:3]
                      for provider in PROVIDERS}
            examples.append(entity+' (used by '+', '.join(sorted(paths))+'; observed '+json.dumps(observed,sort_keys=True)+')')
        examples='; '.join(examples)
        raise ValueError(f'Identity has no real provider ID: {kind}_ids, {len(used)} used identities: {examples}. '
                         'All observed provider links were absent, invalid, withheld or assigned to another identity; '
                         'restore an approved provider mapping before retrying. Previous dataset preserved.')
    audit=[]
    metadata={row['entity_id']:row for row in details}
    for entity in sorted(orphaned):
        # Retain the complete original evidence in the checksum-covered manifest.
        originals={json.dumps(row,ensure_ascii=False,sort_keys=True) for row in by_entity[entity]}
        audit.append({'kind':kind,'entity_id':entity,'reason':'no_accepted_provider_alias',
                      'metadata':metadata[entity],'references':[json.loads(row) for row in sorted(originals)]})
    return ([row for row in ids if row['entity_id'] not in orphaned],
            [row for row in details if row['entity_id'] not in orphaned],audit)

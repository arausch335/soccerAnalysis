"""Quarantine legacy team references whose SofaScore IDs were labeled FotMob.

Use typed fixture evidence and exact names. Never merge canonical IDs or infer
provider equivalence from a coincident numeric ID or a name alone.
"""
from collections import defaultdict
import json

from providers.identity import team_key


def repair(references,fixtures,registry=None):
    references=[dict(row) for row in references]
    by_sofa=defaultdict(set);names=defaultdict(set);used=set();fotmob_used=set()
    for row in fixtures:
        entity=row.get('entity_id','');sofa=row.get('sofascore_id','');fotmob=row.get('fotmob_id','')
        used.add(entity)
        if fotmob:fotmob_used.add(fotmob)
        if entity and sofa and fotmob and sofa!=fotmob:by_sofa[sofa].add((entity,fotmob))
        for key in ('team_name','entity_name','sofascore_name','fotmob_name','reep_name','whoscored_name'):
            if row.get(key):names[entity].add(team_key(row[key]))
    for row in references:
        if row.get('sofascore_id'):
            for key in ('team_name','entity_name','sofascore_name','fotmob_name','reep_name','whoscored_name'):
                if row.get(key):names[row.get('entity_id')].add(team_key(row[key]))
    quarantined={}
    for row in references:
        entity=row.get('entity_id','');native=row.get('fotmob_id','')
        if not entity or row.get('sofascore_id') or not native or entity in used or native in fotmob_used:continue
        targets=by_sofa.get(native,set())
        if len(targets)!=1:continue
        target,correct_fotmob=next(iter(targets))
        supplied={team_key(row[key]) for key in ('team_name','entity_name','fotmob_name','reep_name') if row.get(key)}
        if not supplied.intersection(names[target]):continue
        quarantined[entity]={'entity_id':entity,'name':row.get('team_name') or row.get('entity_name') or row.get('fotmob_name'),
            'incorrect_provider':'fotmob','incorrect_id':native,'verified_entity_id':target,
            'verified_sofascore_id':native,'verified_fotmob_id':correct_fotmob,
            'reason':'sofascore_id_labeled_as_fotmob','original_reference':row}
    if not quarantined:return references,[]
    audits=defaultdict(list)
    for item in quarantined.values():
        audits[item['verified_entity_id']].append(item)
        if registry:registry.withhold('team','fotmob',item['incorrect_id'],'sofascore_id_labeled_as_fotmob')
    result=[]
    for row in references:
        if row.get('entity_id') in quarantined:continue
        if row.get('entity_id') in audits:
            row['namespace_quarantine']=json.dumps(audits[row['entity_id']],ensure_ascii=False,sort_keys=True)
        result.append(row)
    # Preserve evidence even when the only good references came from fixtures.
    present={row.get('entity_id') for row in result}
    for row in fixtures:
        entity=row.get('entity_id')
        if entity in audits and entity not in present:
            result.append({**row,'namespace_quarantine':json.dumps(audits[entity],ensure_ascii=False,sort_keys=True)})
            present.add(entity)
    return result,list(quarantined.values())


def valid_native_team_id(value):
    """All three supported providers use positive numeric team identifiers."""
    value=str(value or '')
    return bool(value and value.isascii() and value.isdecimal() and value.strip('0'))


def clean_provider_ids(row):
    """Retain invalid imported values as audit evidence, never usable IDs."""
    from providers.teamProfiles import canonical_team_id
    row=dict(row);invalid={}
    for provider in ('sofascore','fotmob','whoscored'):
        key=provider+'_id';value=canonical_team_id(provider,str(row.get(key,'')).strip())
        if value and not valid_native_team_id(value):
            invalid[key]=value;row[key]=''
        else:row[key]=value
    if invalid:row['invalid_provider_ids']=json.dumps(invalid,ensure_ascii=False,sort_keys=True)
    return row


def quarantine_invalid_registry_ids(registry):
    if registry is None:return 0
    count=0
    for token in list(registry.value['aliases']):
        kind,provider,native=token.split('|',2)
        if kind=='team' and not valid_native_team_id(native):
            registry.withhold(kind,provider,native,'invalid_native_team_id');count+=1
    return count


def reviewed_conflicts(value,depth=0):
    """Flatten the JSON audit values accumulated across published references."""
    if depth>24:raise ValueError('Reviewed team conflict audit is too deeply nested')
    if not value:return []
    if isinstance(value,str):return reviewed_conflicts(json.loads(value),depth+1)
    if isinstance(value,list):return [row for item in value for row in reviewed_conflicts(item,depth+1)]
    return [value] if isinstance(value,dict) and value.get('provider') else []

"""Recover provider team aliases from matched fixture sides, without changing entities."""
from collections import defaultdict

from providers.reep import PROVIDERS
from .teamNamespace import valid_native_team_id


def _text(value):
    value='' if value is None else str(value).strip()
    return value[:-2] if value.endswith('.0') and value[:-2].isdigit() else value


def recover_fixture_aliases(rows,registry):
    """A single accepted side supplies a side-independent team bridge.

    Peer providers require a validated fixture and an already-resolved match.
    Collect all claims before adding anything; contradictory claims never win
    by frequency, order or club name. Approved/withheld mappings stay authoritative.
    """
    if registry is None:return {'recovered':[],'conflicts':[]}
    aliases=defaultdict(set)
    for token,alias in registry.value['aliases'].items():
        if token.startswith('team|'):aliases[registry.canonical(alias)].add(alias)
    claims={}
    for row in rows:
        match=_text(row.get('entity_id'));baseline=row.get('baseline_provider') or 'sofascore'
        pair=[_text(row.get(side+'_entity_id')) for side in ('home','away')]
        if not match or not all(pair) or pair[0]==pair[1]:continue
        for provider in PROVIDERS:
            native_match=_text(row.get(provider+'_match_id'))
            if not native_match or registry.lookup('match',provider,native_match)!=match:continue
            if provider!=baseline and row.get(provider+'_fixture_check')!='validated':continue
            for side,entity in zip(('home','away'),pair):
                safe=str(row.get(side+'_safe_for_cross_provider_joins','')).lower()
                if provider!=baseline and (safe=='false' or row.get(side+'_team_identity_status')=='conflict'):continue
                native=_text(row.get(provider+'_'+side+'_team_id'))
                if not valid_native_team_id(native) or entity not in aliases:continue
                key=(provider,native)
                held=claims.setdefault(key,{'entities':set(),'home':0,'away':0,'examples':[]})
                held['entities'].add(entity);held[side]+=1
                if len(held['examples'])<3:held['examples'].append({'match_entity_id':match,'provider_match_id':native_match,'side':side})
    recovered=[];conflicts=[]
    for (provider,native),claim in sorted(claims.items()):
        token=f'team|{provider}|{native}';targets=claim['entities']
        if len(targets)!=1:reason='conflicting_fixture_sides'
        elif token in registry.value.get('withheld',{}):reason='withheld_provider_mapping'
        else:
            entity=next(iter(targets));direct=registry.value['aliases'].get(token)
            direct_entity=registry.canonical(direct) if direct else ''
            resolved=registry.lookup('team',provider,native)
            if any(value and value!=entity for value in (direct_entity,resolved)):reason='existing_mapping_disagrees'
            elif direct_entity==entity:continue
            else:
                # Bind to an existing Alex alias only. No canonical ID is minted,
                # merged, renamed or redirected by this recovery.
                alias=min(aliases[entity]);registry.value['aliases'][token]=alias
                if provider=='sofascore':registry.baselines.setdefault(registry.follow(alias),set()).add(native)
                recovered.append({'provider':provider,'provider_team_id':native,'entity_team_id':entity,
                    'home_evidence':claim['home'],'away_evidence':claim['away'],'examples':claim['examples']})
                continue
        conflicts.append({'provider':provider,'provider_team_id':native,'candidate_entities':sorted(targets),
                          'reason':reason,'examples':claim['examples']})
    return {'recovered':recovered,'conflicts':conflicts}


def registry_team_aliases(registry):
    result=defaultdict(lambda:defaultdict(set))
    if registry:
        for token,alias in registry.value['aliases'].items():
            kind,provider,native=token.split('|',2)
            if (kind=='team' and valid_native_team_id(native) and token not in registry.value.get('withheld',{})
                    and registry.lookup('team',provider,native)==registry.canonical(alias)):
                result[registry.canonical(alias)][provider+'_id'].add(native)
    return result

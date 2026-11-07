"""Recover missing WhoScored player aliases from the same matched lineup side."""
from collections import defaultdict
import json
from pathlib import Path
import re

from providers.cache import ResponseCache
from providers.identity import name_key, REJECTED_PLAYER_ID_LINKS, REVIEWED_PLAYER_IDS
from providers.identityReview import player_link, rejected_player_links
from providers.reep import index


def recover_lineup_aliases(root, rows, players, registry):
    """Attach evidence to existing identities; never mint or merge a player ID."""
    recovered, conflicts = [], []
    if registry is None:return {'recovered':recovered,'conflicts':conflicts}
    root=Path(root);allowed={r['entity_id'] for r in players if r.get('entity_id')}
    aliases=defaultdict(set)
    for token,alias in registry.value['aliases'].items():
        if token.startswith('player|') and registry.canonical(alias) in allowed:
            aliases[registry.canonical(alias)].add(alias)
    fixtures=defaultdict(list)
    for row in rows:
        competition=re.sub(r'[^a-z0-9]+','-',str(row.get('competition','')).casefold()).strip('-')
        fixtures[competition,str(row.get('season','')).replace('/','-')].append(row)
    claims=defaultdict(lambda: {'entities':set(),'examples':[]})
    rejected=REJECTED_PLAYER_ID_LINKS|rejected_player_links(root)
    bridges=index(root)
    for source in sorted(root.glob('*/*/config.json')):
        source=source.parent;cache=ResponseCache(source/'raw')
        for row in fixtures[source.parent.name,source.name]:
            match=str(row.get('entity_id') or '')
            sofa=str(row.get('sofascore_match_id') or '')
            who=str(row.get('whoscored_match_id') or '')
            if (not match or not sofa or not who or row.get('whoscored_fixture_check')!='validated'
                    or registry.lookup('match','sofascore',sofa)!=match
                    or registry.lookup('match','whoscored',who)!=match):continue
            paths=[cache.path('sofascore','lineups/'+sofa),cache.path('whoscored','match/'+who)]
            if not all(p.is_file() for p in paths):continue
            saved=[json.loads(p.read_text(encoding='utf-8')) for p in paths]
            if any(s.get('provider')!=p or s.get('key')!=k for s,p,k in zip(
                    saved,('sofascore','whoscored'),('lineups/'+sofa,'match/'+who))):
                raise ValueError('Player bridge raw response identity mismatch')
            baseline=saved[0].get('payload') or {};payload=saved[1].get('payload') or {}
            centre=payload.get('matchCentreData') or {}
            if (str(payload.get('match_id'))!=who or centre.get('statusCode')!=6
                    or str(centre.get('startDate',''))[:10]!=str(row.get('date',''))[:10]):continue
            for side in ('home','away'):
                team=row.get(side+'_entity_id');native_team=str(row.get('sofascore_'+side+'_team_id') or '')
                other=centre.get(side) or {}
                if (not team or registry.lookup('team','sofascore',native_team)!=team
                        or registry.lookup('team','whoscored',str(other.get('teamId') or ''))!=team):continue
                candidates=defaultdict(dict);natives=defaultdict(dict)
                for entry in baseline.get(side,{}).get('players',[]):
                    player=entry.get('player') or {};pid=str(player.get('id') or '')
                    entity=registry.lookup('player','sofascore',pid)
                    name=name_key(player.get('name') or '')
                    if name and entity in aliases:candidates[name][pid]=entity
                for entry in other.get('players',[]):
                    pid=str(entry.get('playerId') or '');name=name_key(entry.get('name') or '')
                    if pid.isdigit() and name:natives[name][pid]=entry.get('name','')
                for name,people in natives.items():
                    targets=candidates.get(name,{})
                    if not targets:continue
                    example={'match_entity_id':match,'provider_match_id':who,'side':side}
                    if len(people)!=1 or len(targets)!=1:
                        conflicts.append({'provider_player_ids':sorted(people),'reason':'ambiguous_lineup_name',**example})
                        continue
                    native=next(iter(people));baseline_id,entity=next(iter(targets.items()))
                    approved=player_link('whoscored',native,root) or REVIEWED_PLAYER_IDS.get(('whoscored',native))
                    if (('whoscored',native,baseline_id) in rejected
                            or (approved is not None and str(approved)!=baseline_id)
                            or bridges.same('player','whoscored',native,'sofascore',baseline_id) is False):
                        conflicts.append({'provider_player_id':native,'reason':'rejected_or_conflicting_player_link',**example})
                        continue
                    claim=claims[native];claim['entities'].add(entity)
                    if len(claim['examples'])<3:claim['examples'].append(example)
    # Collect every fixture claim before accepting a global provider alias.
    for native,claim in sorted(claims.items()):
        token='player|whoscored|'+native;targets=claim['entities']
        if len(targets)!=1:reason='conflicting_lineup_identities'
        elif token in registry.value.get('withheld',{}):reason='withheld_provider_mapping'
        else:
            entity=next(iter(targets));existing=registry.lookup('player','whoscored',native)
            if existing and existing!=entity:reason='existing_mapping_disagrees'
            elif existing==entity:continue
            else:
                registry.value['aliases'][token]=min(aliases[entity])
                recovered.append({'provider':'whoscored','provider_player_id':native,
                    'entity_player_id':entity,'examples':claim['examples']})
                continue
        conflicts.append({'provider_player_id':native,'candidate_entities':sorted(targets),
                          'reason':reason,'examples':claim['examples']})
    return {'recovered':recovered,'conflicts':conflicts}

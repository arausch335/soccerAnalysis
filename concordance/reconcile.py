"""Shared evidence rules for fixture, team and player references."""
from collections import defaultdict
import json

from providers.reep import PROVIDERS
from providers.teamProfiles import canonical_team_id


def rejected_links(kind,observations,bridges):
    """Withhold competing or reused peers unless independently published."""
    forward=defaultdict(set);reverse=defaultdict(set);items=[]
    for native in observations:
        values={p:str(native.get(p+'_id') or '') for p in PROVIDERS}
        if kind=='team':values={p:canonical_team_id(p,v) if v else '' for p,v in values.items()}
        anchor=next(((p,v) for p,v in values.items() if v),None)
        if anchor is None:continue
        anchor_reep={bridges.canonical_id(v) for v in bridges.ids(kind,*anchor)}
        identity=next(iter(anchor_reep)) if len(anchor_reep)==1 else anchor
        for p,v in values.items():
            if not v:continue
            forward[identity,p].add(v);reverse[p,v].add(identity)
            items.append((anchor,identity,p,v))
    rejected=set();verified=set()
    for anchor,identity,p,v in items:
        links={bridges.canonical_id(x) for x in bridges.ids(kind,p,v)}
        token=kind,p,v
        if len(links)==1 and identity in links:verified.add(token)
        # A provider-only observation is not allowed to veto a proven baseline
        # association merely because its richer context was absent locally.
        identities={x for x in reverse[p,v] if isinstance(x,str) or x[0]=='sofascore'}
        if (len(forward[identity,p])>1 or len(identities)>1) and (p,v)!=anchor and token not in verified:
            rejected.add(token)
    return rejected,verified


def annotate_names(rows):
    groups=defaultdict(set)
    for row in rows:
        if row.get('player_name'):groups[row['entity_id']].add(row['player_name'])
    for row in rows:
        if row.get('player_name'):row['name_aliases']=json.dumps(sorted(groups[row['entity_id']]),ensure_ascii=False)

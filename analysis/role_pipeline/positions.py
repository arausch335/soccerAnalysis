"""Detailed positions from cached provider formation slots, independent of event geography."""
import json
from collections import Counter,defaultdict
from pathlib import Path
import pandas as pd
from .config import POSITION_MAP

def position_usage(raw_dir,identities):
    match_map=identities[['provider_match_id','match_id']].drop_duplicates()
    if match_map.provider_match_id.duplicated().any():raise ValueError('Ambiguous provider match mapping')
    match_map=dict(zip(match_map.provider_match_id.astype(int),match_map.match_id.astype(int)))
    payloads={}
    for f in sorted(Path(raw_dir).glob('*.json')):
        raw=json.loads(f.read_text());p=raw.get('payload',raw)
        if not isinstance(p,dict) or 'matchCentreData' not in p:continue
        mid=p.get('match_id',p['matchCentreData'].get('matchId'))
        if mid in match_map:payloads[mid]=p['matchCentreData']
    slot_labels=defaultdict(Counter)
    for c in payloads.values():
        for side in ['home','away']:
            team=c[side];players={int(p['playerId']):p for p in team['players']}
            formations=team.get('formations',[])
            if not formations:continue
            initial=formations[0]
            for pid,slot in zip(initial['playerIds'],initial['formationSlots']):
                label=players.get(pid,{}).get('position')
                if slot and label in POSITION_MAP:slot_labels[(str(initial['formationName']),int(slot))][label]+=1
    # Require complete semantic agreement; no majority-vote guess for ambiguous slots.
    mapping={k:next(iter(v)) for k,v in slot_labels.items() if len(v)==1}
    rows=[];stints=[]
    for provider_mid,c in payloads.items():
        mid=match_map[provider_mid]
        for side in ['home','away']:
            team=c[side];usage=defaultdict(Counter);labels=defaultdict(Counter)
            segments=sorted(team.get('formations',[]),key=lambda f:float(f['startMinuteExpanded']))
            for idx,f in enumerate(segments):
                start=float(f['startMinuteExpanded']);end=float(f['endMinuteExpanded'])
                # Avoid counting an overlap twice if a provider segment extends beyond the next start.
                if idx+1<len(segments):end=min(end,float(segments[idx+1]['startMinuteExpanded']))
                if end<=start:continue
                for pid,slot in zip(f['playerIds'],f['formationSlots']):
                    if not slot:continue
                    label=mapping.get((str(f['formationName']),int(slot)))
                    group=POSITION_MAP.get(label,'Unknown')
                    usage[int(pid)][group]+=end-start;labels[int(pid)][label or 'Unknown']+=end-start
                    stints.append({'match_id':mid,'provider_player_id':int(pid),'team_id':team['teamId'],
                        'start_minute_expanded':start,'end_minute_expanded':end,'formation':str(f['formationName']),
                        'slot':slot,'position_label':label,'position_group':group,'source':'whoscored_formation_slot'})
            for p in team['players']:
                pid=int(p['playerId']);u=usage[pid];total=sum(u.values())
                known={k:v for k,v in u.items() if k!='Unknown'}
                if known and total:
                    dominant=max(known,key=known.get);share=known[dominant]/total;source='whoscored_formation_stints'
                elif p.get('isFirstEleven') and p['position'] in POSITION_MAP:
                    dominant=POSITION_MAP[p['position']];share=1.;source='whoscored_starting_position_only'
                else:dominant='Unknown';share=0.;source='unavailable'
                rows.append({'match_id':mid,'provider_player_id':pid,'provider_team_id':team['teamId'],
                    'detailed_position':dominant,'dominant_position_share':share,
                    'position_source':source,'starting_position_label':p.get('position'),
                    'position_usage_json':json.dumps(dict(u),sort_keys=True),'formation_span_minutes':total})
    return pd.DataFrame(rows),pd.DataFrame(stints),[
        {'formation':k[0],'slot':k[1],'position_label':label,'position_group':POSITION_MAP[label],
         'evidence_count':sum(slot_labels[k].values())} for k,label in mapping.items()]

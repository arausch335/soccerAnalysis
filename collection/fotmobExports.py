"""FotMob exports retain native shot coordinates and complete source statistics.

Shot coordinates stay in FotMob units; lineup layout positions are not tracking
data and are never substituted for touch heatmaps or passing/dribbling locations.
"""
import json
from pathlib import Path

from providers.statistics import number
from .jsonFiles import read_json
from .selection import season_label

SHOT_FEATURE_COLUMNS = (
    'fotmob_shotmap_status','fotmob_shotmap_reconciled','fotmob_shot_count',
    'fotmob_shot_xg_sum','fotmob_shot_xgot_sum','fotmob_shot_mean_x_raw',
    'fotmob_shot_mean_y_raw','fotmob_headed_shots','fotmob_left_foot_shots',
    'fotmob_right_foot_shots','fotmob_penalty_shots',
)


def match_payload(match,directory):
    relative=match.metadata.get('raw_response_files',{}).get('fotmob')
    if not relative:return None
    path=Path(directory)/relative
    if not path.exists():return None
    saved=read_json(path)
    expected=match.metadata.get('provider_match_ids',{}).get('fotmob')
    if saved.get('provider')!='fotmob' or saved.get('key')!=f'match/{expected}':
        raise ValueError('FotMob export cache identity mismatch')
    return saved


def shot_features(saved,player_id,team_id,appeared,published_shots):
    result=dict.fromkeys(SHOT_FEATURE_COLUMNS)
    result['fotmob_shotmap_status']='unavailable'
    if not appeared:
        result['fotmob_shotmap_status']='not_applicable_unused_sub'
        return result
    content=(saved or {}).get('payload',{}).get('content',{})
    shots=(content.get('shotmap') or {}).get('shots')
    if not isinstance(shots,list) or player_id is None or team_id is None:return result
    shots=[s for s in shots if s.get('playerId')==player_id and s.get('teamId')==team_id
           and not s.get('isOwnGoal',False)]
    result.update(fotmob_shotmap_status='available',fotmob_shot_count=len(shots),
                  fotmob_shotmap_reconciled=len(shots)==published_shots if published_shots is not None else None)
    for field,column in [('expectedGoals','fotmob_shot_xg_sum'),
                         ('expectedGoalsOnTarget','fotmob_shot_xgot_sum')]:
        values=[number(s.get(field)) for s in shots]
        if all(v is not None for v in values):result[column]=sum(values)
    for field in ('x','y'):
        values=[number(s.get(field)) for s in shots]
        if values and all(v is not None for v in values):
            result[f'fotmob_shot_mean_{field}_raw']=sum(values)/len(values)
    for value,column in [('Header','fotmob_headed_shots'),('LeftFoot','fotmob_left_foot_shots'),
                         ('RightFoot','fotmob_right_foot_shots')]:
        if all(s.get('shotType') is not None for s in shots):
            result[column]=sum(s['shotType']==value for s in shots)
    if all(s.get('situation') is not None for s in shots):
        result['fotmob_penalty_shots']=sum(s['situation']=='Penalty' for s in shots)
    return result


def csv_value(value):
    return json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value


def source_rows(match,saved):
    """Match-level exports also retain playerStats entries outside the lineup."""
    if saved is None:return [],[],[]
    payload=saved['payload'];content=payload.get('content',{})
    context={'match_id':match.id,'date':match.kickoff.date().isoformat(),
             'season':season_label(match.season.name),'competition':match.season.competition.name,
             'provider':'fotmob',
             'provider_match_id':int(payload['general']['matchId']),
             'fetched_at':saved.get('fetched_at')}
    shots=[{**context,**{key:csv_value(value) for key,value in shot.items()}}
           for shot in (content.get('shotmap') or {}).get('shots',[])]
    stats=[]
    for pid,player in (content.get('playerStats') or {}).items():
        for group in player.get('stats',[]):
            for label,item in group.get('stats',{}).items():
                if not item.get('key'):continue  # Embedded shotmap is exported above.
                stats.append({**context,'provider_player_id':int(pid),'player_name':player.get('name'),
                    'provider_team_id':player.get('teamId'),'group':group.get('title'),
                    'label':label,'field':item['key'],'raw_value':json.dumps(item['stat'],ensure_ascii=False)})
    teams=[]
    for period,section in (content.get('stats') or {}).get('Periods',{}).items():
        for group in section.get('stats',[]):
            for item in group.get('stats',[]):
                if item.get('type')=='title':continue
                for side,value in zip(('home','away'),item.get('stats',[])):
                    team=payload.get('general',{}).get(side+'Team',{})
                    teams.append({**context,'side':side,'provider_team_id':team.get('id'),
                        'team':team.get('name'),
                        'matchweek':match.round_number,
                        'period':period,'group':group.get('key'),
                        'field':item.get('key'),'label':item.get('title'),
                        'value':csv_value(value),'format':item.get('format'),'type':item.get('type')})
    return shots,stats,teams

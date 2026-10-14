"""Lossless-enough, one-row-per-event export for cached WhoScored matches."""
import json
from pathlib import Path
from .jsonFiles import read_json
from .selection import season_label


EVENT_COLUMNS = (
    'match_id','provider_match_id','date','season','competition','home_team','away_team','fetched_at',
    'event_index','provider_event_id','event_id','period','period_name','minute','second',
    'expanded_minute','team_id','team_name','player_id','player_name','related_event_id',
    'related_player_id','related_player_name','event_type','event_type_id','outcome','outcome_id',
    'x','y','end_x','end_y','blocked_x','blocked_y','goal_mouth_y','goal_mouth_z',
    'is_touch','is_shot','is_goal','is_own_goal','is_key_pass','card_type','card_type_id','body_part',
    'situation','zone','pass_length','pass_angle','qualifier_names','qualifiers_json',
    'satisfied_event_types','satisfied_event_type_ids','extra_event_json',
)

# Every top-level field observed in the event schema is represented by a stable
# column above. If WhoScored adds another field later, keep it in a compact JSON
# cell until the exporter is updated instead of silently dropping it.
MAPPED_EVENT_FIELDS = {
    'id','eventId','minute','second','expandedMinute','teamId','playerId','relatedEventId',
    'relatedPlayerId','period','type','outcomeType','x','y','endX','endY','blockedX','blockedY',
    'goalMouthY','goalMouthZ','isTouch','isShot','isGoal','isOwnGoal','isKeyPass','cardType',
    'qualifiers','satisfiedEventsTypes',
}


def match_payload(match,directory):
    relative=match.metadata.get('raw_response_files',{}).get('whoscored')
    if not relative:return None
    path=Path(directory)/relative
    if not path.exists():return None
    saved=read_json(path)
    expected=match.metadata.get('provider_match_ids',{}).get('whoscored')
    if saved.get('provider')!='whoscored' or saved.get('key')!=f'match/{expected}':
        raise ValueError('WhoScored export cache identity mismatch')
    return saved


def _display(value):
    return value.get('displayName') if isinstance(value,dict) else None


def _value(value):
    return value.get('value') if isinstance(value,dict) else None


def event_rows(match,saved):
    if saved is None:return []
    payload=saved['payload'];data=payload.get('matchCentreData',{})
    events=data.get('events')
    if not isinstance(events,list):return []
    teams={data.get(side,{}).get('teamId'):data.get(side,{}).get('name') for side in ('home','away')}
    players={player.get('playerId'):player.get('name') for side in ('home','away')
             for player in data.get(side,{}).get('players',[])}
    type_names={value:name for name,value in payload.get('matchCentreEventTypeJson',{}).items()}
    context={'match_id':match.id,
             'provider_match_id':match.metadata.get('provider_match_ids',{}).get('whoscored'),
             'date':match.kickoff.date().isoformat(),'season':season_label(match.season.name),
             'competition':match.season.competition.name,
             'home_team':match.home_team.name,'away_team':match.away_team.name,
             'fetched_at':saved.get('fetched_at')}
    rows=[]
    for index,event in enumerate(events):
        qualifiers=event.get('qualifiers') if isinstance(event.get('qualifiers'),list) else []
        qualifier_names=[_display(q.get('type')) for q in qualifiers]
        qualifier_values={name:q.get('value') for q,name in zip(qualifiers,qualifier_names) if name}
        satisfied_ids=event.get('satisfiedEventsTypes') if isinstance(event.get('satisfiedEventsTypes'),list) else []
        satisfied_names=[type_names.get(value,str(value)) for value in satisfied_ids]
        def first(names):
            return next((name for name in names if name in qualifier_names),None)
        card=_display(event.get('cardType')) or first(('Yellow','SecondYellow','Red'))
        row={**context,
            'event_index':index,'provider_event_id':event.get('id'),'event_id':event.get('eventId'),
            'period':_value(event.get('period')),'period_name':_display(event.get('period')),
            'minute':event.get('minute'),'second':event.get('second'),'expanded_minute':event.get('expandedMinute'),
            'team_id':event.get('teamId'),'team_name':teams.get(event.get('teamId')),
            'player_id':event.get('playerId'),'player_name':players.get(event.get('playerId')),
            'related_event_id':event.get('relatedEventId'),'related_player_id':event.get('relatedPlayerId'),
            'related_player_name':players.get(event.get('relatedPlayerId')),
            'event_type':_display(event.get('type')),'event_type_id':_value(event.get('type')),
            'outcome':_display(event.get('outcomeType')),'outcome_id':_value(event.get('outcomeType')),
            'x':event.get('x'),'y':event.get('y'),'end_x':event.get('endX'),'end_y':event.get('endY'),
            'blocked_x':event.get('blockedX'),'blocked_y':event.get('blockedY'),
            'goal_mouth_y':event.get('goalMouthY'),'goal_mouth_z':event.get('goalMouthZ'),
            'is_touch':event.get('isTouch'),'is_shot':event.get('isShot'),'is_goal':event.get('isGoal'),
            'is_own_goal':event.get('isOwnGoal'),
            'is_key_pass':event.get('isKeyPass') is True or 'passKey' in satisfied_names or 'KeyPass' in qualifier_names,
            'card_type':card,'card_type_id':_value(event.get('cardType')),
            'body_part':first(('RightFoot','LeftFoot','Head','OtherBodyPart')),
            'situation':first(('RegularPlay','SetPiece','FromCorner','DirectFreekick','Penalty','FastBreak')),
            'zone':qualifier_values.get('Zone'),'pass_length':qualifier_values.get('Length'),
            'pass_angle':qualifier_values.get('Angle'),
            'qualifier_names':json.dumps(qualifier_names,ensure_ascii=False),
            'qualifiers_json':json.dumps(qualifiers,ensure_ascii=False),
            'satisfied_event_types':json.dumps(satisfied_names,ensure_ascii=False),
            'satisfied_event_type_ids':json.dumps(satisfied_ids,ensure_ascii=False),
            'extra_event_json':json.dumps({key:value for key,value in event.items()
                                           if key not in MAPPED_EVENT_FIELDS},ensure_ascii=False),
        }
        rows.append(row)
    return rows

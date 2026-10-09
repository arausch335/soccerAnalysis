"""Versioned, provider-grounded features derived from Sofascore touch heatmaps."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from statistics import median
from .jsonFiles import read_json

from pitch_zones import (
    LATERAL_BINS, LEFT_HALF_SPACE_MIN, LEFT_WING_MIN, LONGITUDINAL_BINS,
    RIGHT_HALF_SPACE_MAX, RIGHT_WING_MAX, channel as pitch_channel,
    lateral_index, longitudinal_index, zone_number,
)

FEATURE_VERSION = 'heatmap_v3_box_aligned_zones'
PASS_FEATURE_VERSION = 'passmap_v3_box_aligned_zones'
DRIBBLE_FEATURE_VERSION = 'dribblemap_v3_box_aligned_zones'
IMPORTANT_WEIGHTS = {'key_pass': 4.0, 'shot': 3.0}

FEATURE_COLUMNS = (
    'spatial_feature_version','heatmap_source','heatmap_status','average_position_source','touch_count',
    'average_x_raw','average_y_raw','average_forwardness','average_signed_width','average_absolute_width',
    'median_forwardness','median_signed_width','median_absolute_width','forward_variability',
    'lateral_variability','radial_dispersion','position_ellipse_area','spatial_entropy','occupied_area_share',
    'dominant_zone','dominant_zone_touch_share','dominant_zone_number',
    'dominant_zone_number_touch_share','hotspot_zone_number','hotspot_forwardness','hotspot_signed_width',
    'defensive_third_share','middle_third_share','final_third_share','left_wing_share',
    'left_half_space_share','central_share','right_half_space_share','right_wing_share',
    'opposition_box_share','important_actions_status','important_action_count','important_action_weight_total',
    'important_action_forwardness','important_action_signed_width','important_action_absolute_width',
    'important_action_final_third_share','important_action_box_share','important_action_distance_from_touch_centroid',
)

PASS_FEATURE_COLUMNS = (
    'pass_feature_version','pass_map_source','pass_map_status','pass_attempt_count','pass_completion_count',
    'pass_completion_rate','average_origin_forwardness','average_origin_signed_width',
    'average_origin_absolute_width','average_destination_forwardness','average_destination_signed_width',
    'average_destination_absolute_width','average_pass_length_m','average_forward_gain_m',
    'average_lateral_displacement_m','average_goal_distance_reduction_m','total_goal_distance_reduction_m',
    'completed_goal_distance_reduction_m','forward_attempt_share','backward_attempt_share',
    'lateral_attempt_share','progressive_attempt_share','long_pass_attempt_share','switch_attempt_share',
    'forward_completion_rate','progressive_completion_rate','long_pass_completion_rate','switch_completion_rate',
    'final_third_entry_attempts','final_third_entry_completions','final_third_entry_completion_rate',
    'box_entry_attempts','box_entry_completions','box_entry_completion_rate','pass_map_key_passes',
    'dominant_origin_zone','dominant_origin_zone_number','dominant_destination_zone',
    'dominant_destination_zone_number','origin_zone_entropy','destination_zone_entropy',
)

PASS_RECORD_COLUMNS = (
    'pass_index','origin_x_raw','origin_y_raw','destination_x_raw','destination_y_raw',
    'origin_forwardness','origin_signed_width','destination_forwardness','destination_signed_width',
    'pass_length_m','forward_gain_m','lateral_displacement_m','goal_distance_reduction_m','direction',
    'progressive','long_ball','switch','final_third_entry','box_entry','key_pass','completed',
    'event_action_type','origin_zone','origin_zone_number','destination_zone','destination_zone_number',
)

PASS_TRANSITION_COLUMNS = (
    'origin_zone','origin_zone_number','destination_zone','destination_zone_number',
    'attempts','completed','completion_rate',
)

DRIBBLE_FEATURE_COLUMNS = (
    'dribble_feature_version','dribble_map_source','dribble_map_status','dribble_reconciliation_status',
    'published_dribble_attempts','published_dribbles_completed','dribble_attempts','mapped_dribbles_completed',
    'dribble_completion_rate','dribble_attempts_per_90','dribbles_completed_per_90',
    'dribble_attempts_per_100_touches','dribbles_completed_per_100_touches',
    'average_dribble_forwardness','average_dribble_signed_width','average_dribble_absolute_width',
    'dribble_location_variability','dribble_location_entropy','dominant_dribble_zone',
    'dominant_dribble_zone_number',
    'defensive_third_attempt_share','middle_third_attempt_share','final_third_attempt_share',
    'central_attempt_share','wide_attempt_share','box_attempt_share','final_third_completion_rate',
    'central_completion_rate','wide_completion_rate','box_completion_rate','successful_dribble_forwardness',
    'successful_dribble_signed_width','failed_dribble_forwardness','failed_dribble_signed_width',
    'dribble_centroid_distance_from_touch_centroid','dribble_forwardness_minus_touch_forwardness',
    'dribble_width_minus_touch_width','average_dribble_goal_proximity','successful_dribble_danger',
    'failed_dribble_danger','danger_weighted_completion_rate',
)

DRIBBLE_RECORD_COLUMNS = (
    'dribble_index','x_raw','y_raw','forwardness','signed_width','absolute_width','zone','zone_number','successful',
    'defensive_third','middle_third','final_third','central','wide','opposition_box','goal_proximity',
    'event_action_type',
)

# Portable export contract: raw coordinates plus the smallest useful heatmap
# summary. All tactical geometry is intentionally left to downstream analysis.
MINIMAL_SPATIAL_COLUMNS = (
    'touch_count','average_x','average_y',
)
RAW_PASS_RECORD_COLUMNS = (
    'pass_index','origin_x_raw','origin_y_raw','destination_x_raw','destination_y_raw',
    'completed','key_pass','long_ball','event_action_type','source',
)
RAW_DRIBBLE_RECORD_COLUMNS = (
    'dribble_index','x_raw','y_raw','successful','event_action_type','source',
)


def _empty(status='unavailable'):
    values={name:None for name in FEATURE_COLUMNS}
    values.update(spatial_feature_version=FEATURE_VERSION,heatmap_status=status)
    return values


def _payload(directory,relative):
    if not relative:return None
    path=Path(directory)/relative
    if not path.exists():return None
    saved=read_json(path)
    return saved.get('payload')


def _point(value):
    if not isinstance(value,dict):return None
    x=value.get('x');y=value.get('y')
    if isinstance(x,(int,float)) and not isinstance(x,bool) and isinstance(y,(int,float)) and not isinstance(y,bool):
        if math.isfinite(x) and math.isfinite(y) and 0<=x<=100 and 0<=y<=100:return float(x),float(y)
    return None


def _forward(x):return (x-50.0)/50.0
def _signed_width(y):return (50.0-y)/50.0  # left negative, right positive from the attacking view
def _in_box(x,y):return x>=84.3 and 20.35<=y<=79.65


def _third(x):
    return 'defensive' if x<100/3 else 'middle' if x<200/3 else 'final'


def _channel(y):
    return pitch_channel(y)


def _average_position(payload,player_id):
    if not isinstance(payload,dict):return None
    for side in ('home','away'):
        for row in payload.get(side,[]):
            if row.get('player',{}).get('id')==player_id:
                point=_point({'x':row.get('averageX'),'y':row.get('averageY')})
                if point:return point,'sofascore_average_positions'
    return None


def extract_minimal_spatial(match,appearance,directory):
    """Return only touch count and average position; leave map geometry downstream."""
    files=match.metadata.get('sofascore_spatial_files',{})
    pid=appearance.player.id;player_files=files.get(str(pid),{})
    heatmap=_payload(directory,player_files.get('heatmap'))
    values={name:None for name in MINIMAL_SPATIAL_COLUMNS}
    points=[]
    if isinstance(heatmap,dict) and isinstance(heatmap.get('heatmap'),list):
        points=[point for value in heatmap['heatmap'] if (point:=_point(value))]
        values['touch_count']=len(points)
    average=_average_position(_payload(directory,files.get('average_positions')),pid)
    if average:
        (values['average_x'],values['average_y']),_=average
    elif points:
        values['average_x']=sum(x for x,_ in points)/len(points)
        values['average_y']=sum(y for _,y in points)/len(points)
    return values


def _important_actions(rating,shotmap,player_id):
    result=[];sources=[]
    if isinstance(rating,dict) and isinstance(rating.get('passes'),list):
        sources.append('rating_breakdown')
        for event in rating.get('passes',[]):
            if event.get('keypass'):
                point=_point(event.get('playerCoordinates'))
                if point:result.append((*point,IMPORTANT_WEIGHTS['key_pass']))
    if isinstance(shotmap,dict) and isinstance(shotmap.get('shotmap'),list):
        sources.append('shotmap')
        for event in shotmap.get('shotmap',[]):
            event_player=event.get('player',{}).get('id',event.get('playerId'))
            if event_player==player_id and not event.get('isOwnGoal',False):
                point=_point(event.get('playerCoordinates'))
                if point:result.append((*point,IMPORTANT_WEIGHTS['shot']))
    return result,'+'.join(sources) if sources else 'unavailable'


def extract_spatial_features(match,appearance,directory):
    """Return versioned heatmap columns; unavailable data remains explicitly missing."""
    statuses=match.metadata.get('sofascore_spatial_status',{})
    files=match.metadata.get('sofascore_spatial_files',{})
    pid=appearance.player.id;player_status=statuses.get(str(pid),{});player_files=files.get(str(pid),{})
    result=_empty(player_status.get('heatmap','unavailable'))
    heatmap=_payload(directory,player_files.get('heatmap'))
    if isinstance(heatmap,dict) and isinstance(heatmap.get('heatmap'),list):
        result.update(heatmap_source='sofascore',heatmap_status='complete',touch_count=0)
    points=[point for value in (heatmap or {}).get('heatmap',[]) if (point:=_point(value))]
    rating=_payload(directory,player_files.get('rating_breakdown'))
    shotmap=_payload(directory,files.get('shotmap'))
    important,important_status=_important_actions(rating,shotmap,pid)
    result['important_actions_status']=important_status
    if important_status!='unavailable':
        result['important_action_count']=len(important)
        result['important_action_weight_total']=sum(p[2] for p in important)
    if important:
        total=result['important_action_weight_total']
        ix=sum(x*w for x,y,w in important)/total;iy=sum(y*w for x,y,w in important)/total
        result.update(
            important_action_forwardness=_forward(ix),important_action_signed_width=_signed_width(iy),
            important_action_absolute_width=abs(_signed_width(iy)),
            important_action_final_third_share=sum(w for x,y,w in important if x>=200/3)/total,
            important_action_box_share=sum(w for x,y,w in important if _in_box(x,y))/total)
    if not points:return result

    xs=[p[0] for p in points];ys=[p[1] for p in points];n=len(points)
    fs=[_forward(x) for x in xs];ws=[_signed_width(y) for y in ys]
    mx=sum(xs)/n;my=sum(ys)/n;mf=sum(fs)/n;mw=sum(ws)/n
    average=_average_position(_payload(directory,files.get('average_positions')),pid)
    if average:
        (average_x,average_y),average_source=average
    else:
        average_x,average_y=mx,my;average_source='computed_heatmap_mean'
    vf=sum((v-mf)**2 for v in fs)/n;vw=sum((v-mw)**2 for v in ws)/n
    covariance=sum((f-mf)*(w-mw) for f,w in zip(fs,ws))/n
    ellipse=math.pi*math.sqrt(max(0.0,vf*vw-covariance**2))
    cells=[(longitudinal_index(x),lateral_index(y)) for x,y in points]
    counts=Counter(cells);occupied=len(counts);denominator=math.log(LONGITUDINAL_BINS*LATERAL_BINS)
    entropy=-sum((count/n)*math.log(count/n) for count in counts.values())/denominator if n>1 else 0.0
    dominant_tactical=Counter(f'{_third(x)}_{_channel(y)}' for x,y in points)
    dominant_zone,dominant_count=dominant_tactical.most_common(1)[0]
    hotspot_cell,hotspot_count=counts.most_common(1)[0]
    dominant_zone_number=hotspot_cell[0]*LATERAL_BINS+hotspot_cell[1]+1
    hotspot=[p for p,c in zip(points,cells) if c==hotspot_cell]
    hotspot_x=sum(p[0] for p in hotspot)/hotspot_count;hotspot_y=sum(p[1] for p in hotspot)/hotspot_count
    shares=lambda predicate:sum(predicate(x,y) for x,y in points)/n
    result.update(
        heatmap_status='complete',average_position_source=average_source,touch_count=n,
        average_x_raw=average_x,average_y_raw=average_y,average_forwardness=_forward(average_x),
        average_signed_width=_signed_width(average_y),average_absolute_width=abs(_signed_width(average_y)),
        median_forwardness=median(fs),median_signed_width=median(ws),median_absolute_width=median(map(abs,ws)),
        forward_variability=math.sqrt(vf),lateral_variability=math.sqrt(vw),
        radial_dispersion=math.sqrt(vf+vw),position_ellipse_area=ellipse,spatial_entropy=entropy,
        occupied_area_share=occupied/(LONGITUDINAL_BINS*LATERAL_BINS),dominant_zone=dominant_zone,
        dominant_zone_touch_share=dominant_count/n,dominant_zone_number=dominant_zone_number,
        dominant_zone_number_touch_share=hotspot_count/n,hotspot_zone_number=dominant_zone_number,
        hotspot_forwardness=_forward(hotspot_x),
        hotspot_signed_width=_signed_width(hotspot_y),
        defensive_third_share=shares(lambda x,y:x<100/3),middle_third_share=shares(lambda x,y:100/3<=x<200/3),
        final_third_share=shares(lambda x,y:x>=200/3),left_wing_share=shares(lambda x,y:y>=LEFT_WING_MIN),
        left_half_space_share=shares(lambda x,y:LEFT_HALF_SPACE_MIN<=y<LEFT_WING_MIN),
        central_share=shares(lambda x,y:RIGHT_HALF_SPACE_MAX<=y<LEFT_HALF_SPACE_MIN),
        right_half_space_share=shares(lambda x,y:RIGHT_WING_MAX<=y<RIGHT_HALF_SPACE_MAX),
        right_wing_share=shares(lambda x,y:y<RIGHT_WING_MAX),
        opposition_box_share=shares(_in_box),
    )
    if important:
        total=result['important_action_weight_total']
        ix=sum(x*w for x,y,w in important)/total;iy=sum(y*w for x,y,w in important)/total
        result.update(
            important_action_forwardness=_forward(ix),important_action_signed_width=_signed_width(iy),
            important_action_absolute_width=abs(_signed_width(iy)),
            important_action_final_third_share=sum(w for x,y,w in important if x>=200/3)/total,
            important_action_box_share=sum(w for x,y,w in important if _in_box(x,y))/total,
            important_action_distance_from_touch_centroid=math.hypot(_forward(ix)-mf,_signed_width(iy)-mw),
        )
    return result


def _rate(rows,key='completed'):
    return sum(row[key] for row in rows)/len(rows) if rows else None


def _zone_entropy(zones):
    if not zones:return None
    counts=Counter(zones);n=len(zones)
    return -sum((count/n)*math.log(count/n) for count in counts.values())/math.log(30) if n>1 else 0.0


def _pass_record(index,event):
    start=_point(event.get('playerCoordinates'));end=_point(event.get('passEndCoordinates'))
    if not start or not end:return None
    x,y=start;end_x,end_y=end
    dx_m=(end_x-x)*1.05;dy_m=(end_y-y)*.68
    length=math.hypot(dx_m,dy_m)
    start_goal=math.hypot(105-x*1.05,34-y*.68);end_goal=math.hypot(105-end_x*1.05,34-end_y*.68)
    progression=start_goal-end_goal
    ratio=progression/start_goal if start_goal else 0.0
    progressive=length>=10 and ratio>=(.25 if x<50 else .15)
    direction='forward' if dx_m>=3 else 'backward' if dx_m<=-3 else 'lateral'
    switch=length>=20 and ((y<RIGHT_HALF_SPACE_MAX and end_y>LEFT_HALF_SPACE_MIN)
                           or (y>LEFT_HALF_SPACE_MIN and end_y<RIGHT_HALF_SPACE_MAX))
    return {
        'pass_index':index,'origin_x_raw':x,'origin_y_raw':y,'destination_x_raw':end_x,'destination_y_raw':end_y,
        'origin_forwardness':_forward(x),'origin_signed_width':_signed_width(y),
        'destination_forwardness':_forward(end_x),'destination_signed_width':_signed_width(end_y),
        'pass_length_m':length,'forward_gain_m':dx_m,'lateral_displacement_m':abs(dy_m),
        'goal_distance_reduction_m':progression,'direction':direction,'progressive':progressive,
        'long_ball':bool(event.get('isLongBall',False)),'switch':switch,
        'final_third_entry':x<200/3<=end_x,'box_entry':not _in_box(x,y) and _in_box(end_x,end_y),
        'key_pass':bool(event.get('keypass',False)),'completed':event.get('outcome') is True,
        'event_action_type':event.get('eventActionType'),'origin_zone':f'{_third(x)}_{_channel(y)}',
        'origin_zone_number':zone_number(x,y),'destination_zone':f'{_third(end_x)}_{_channel(end_y)}',
        'destination_zone_number':zone_number(end_x,end_y),
    }



def action_payload(match, appearance, directory, category):
    """Prefer Sofascore maps; use identified WhoScored events if absent.

    WhoScored coordinates are retained in their provider frame. Only explicit
    pass endpoints/outcomes are accepted; no paths are inferred from totals.
    """
    files=match.metadata.get('sofascore_spatial_files',{}).get(str(appearance.player.id),{})
    payload=_payload(directory,files.get('rating_breakdown'))
    if isinstance(payload,dict) and isinstance(payload.get(category),list):
        return payload,'sofascore'
    who=appearance.stats.raw.get('_providers',{}).get('whoscored',{})
    raw=_payload(directory,match.metadata.get('raw_response_files',{}).get('whoscored'))
    if not who or not isinstance(raw,dict):return payload,'sofascore'
    events=raw.get('matchCentreData',{}).get('events')
    if not isinstance(events,list):return payload,'sofascore'
    converted=[];rejected=0
    kind='Pass' if category=='passes' else 'TakeOn'
    for event in events:
        if event.get('playerId')!=who.get('id') or event.get('type',{}).get('displayName')!=kind:continue
        outcome=event.get('outcomeType',{}).get('displayName')
        if outcome not in ('Successful','Unsuccessful'):
            rejected+=1;continue
        point={'x':event.get('x'),'y':event.get('y')}
        if not _point(point):rejected+=1;continue
        item={'playerCoordinates':point,'outcome':outcome=='Successful','eventActionType':kind}
        if category=='passes':
            qualifiers={q.get('type',{}).get('displayName'):q.get('value') for q in event.get('qualifiers',[])}
            try:end={'x':float(qualifiers['PassEndX']),'y':float(qualifiers['PassEndY'])}
            except (KeyError,TypeError,ValueError):rejected+=1;continue
            if not _point(end):rejected+=1;continue
            item.update(passEndCoordinates=end,isLongBall='Longball' in qualifiers,keypass=event.get('isKeyPass') is True)
        converted.append(item)
    return {category:converted,'_rejected':rejected},'whoscored'


def extract_raw_passes(match,appearance,directory):
    """Return only observed pass coordinates and source flags for CSV export."""
    payload,source=action_payload(match,appearance,directory,'passes')
    rows=[]
    for index,event in enumerate((payload or {}).get('passes',[])):
        start=_point(event.get('playerCoordinates'));end=_point(event.get('passEndCoordinates'))
        if not start or not end:continue
        rows.append({'pass_index':index,'origin_x_raw':start[0],'origin_y_raw':start[1],
            'destination_x_raw':end[0],'destination_y_raw':end[1],
            'completed':event.get('outcome') is True,'key_pass':bool(event.get('keypass',False)),
            'long_ball':bool(event.get('isLongBall',False)),
            'event_action_type':event.get('eventActionType'),'source':source})
    return rows


def extract_raw_dribbles(match,appearance,directory):
    """Return only observed take-on coordinates and outcomes for CSV export."""
    payload,source=action_payload(match,appearance,directory,'dribbles')
    rows=[]
    for index,event in enumerate((payload or {}).get('dribbles',[])):
        point=_point(event.get('playerCoordinates'))
        if not point:continue
        rows.append({'dribble_index':index,'x_raw':point[0],'y_raw':point[1],
            'successful':event.get('outcome') is True,
            'event_action_type':event.get('eventActionType'),'source':source})
    return rows

def extract_pass_features(match,appearance,directory):
    """Return a versioned pass summary, long-form rows and zone transitions."""
    files=match.metadata.get('sofascore_spatial_files',{}).get(str(appearance.player.id),{})
    status=match.metadata.get('sofascore_spatial_status',{}).get(str(appearance.player.id),{}).get('rating_breakdown','unavailable')
    result={name:None for name in PASS_FEATURE_COLUMNS}
    result.update(pass_feature_version=PASS_FEATURE_VERSION,pass_map_status=status)
    payload,source=action_payload(match,appearance,directory,'passes')
    if not isinstance(payload,dict) or not isinstance(payload.get('passes'),list):
        if status=='complete':result['pass_map_status']='schema_unavailable'
        return result,[],[]
    result.update(pass_map_source=source,pass_attempt_count=0,pass_completion_count=0,
                  final_third_entry_attempts=0,final_third_entry_completions=0,
                  box_entry_attempts=0,box_entry_completions=0,pass_map_key_passes=0)
    passes=[row for index,event in enumerate(payload['passes']) if (row:=_pass_record(index,event))]
    result['pass_map_status']='partial' if payload.get('_rejected') or len(passes)!=len(payload['passes']) else 'complete';result['pass_attempt_count']=len(passes)
    if not passes:return result,passes,[]
    completed=[row for row in passes if row['completed']]
    subsets={
        'forward':[row for row in passes if row['direction']=='forward'],
        'backward':[row for row in passes if row['direction']=='backward'],
        'lateral':[row for row in passes if row['direction']=='lateral'],
        'progressive':[row for row in passes if row['progressive']],
        'long_pass':[row for row in passes if row['long_ball']],
        'switch':[row for row in passes if row['switch']],
        'final_third_entry':[row for row in passes if row['final_third_entry']],
        'box_entry':[row for row in passes if row['box_entry']],
    }
    n=len(passes);origins=[row['origin_zone'] for row in passes];destinations=[row['destination_zone'] for row in passes]
    origin_numbers=[row['origin_zone_number'] for row in passes]
    destination_numbers=[row['destination_zone_number'] for row in passes]
    result.update(
        pass_completion_count=len(completed),pass_completion_rate=len(completed)/n,
        average_origin_forwardness=sum(row['origin_forwardness'] for row in passes)/n,
        average_origin_signed_width=sum(row['origin_signed_width'] for row in passes)/n,
        average_origin_absolute_width=sum(abs(row['origin_signed_width']) for row in passes)/n,
        average_destination_forwardness=sum(row['destination_forwardness'] for row in passes)/n,
        average_destination_signed_width=sum(row['destination_signed_width'] for row in passes)/n,
        average_destination_absolute_width=sum(abs(row['destination_signed_width']) for row in passes)/n,
        average_pass_length_m=sum(row['pass_length_m'] for row in passes)/n,
        average_forward_gain_m=sum(row['forward_gain_m'] for row in passes)/n,
        average_lateral_displacement_m=sum(row['lateral_displacement_m'] for row in passes)/n,
        average_goal_distance_reduction_m=sum(row['goal_distance_reduction_m'] for row in passes)/n,
        total_goal_distance_reduction_m=sum(row['goal_distance_reduction_m'] for row in passes),
        completed_goal_distance_reduction_m=sum(row['goal_distance_reduction_m'] for row in completed),
        forward_attempt_share=len(subsets['forward'])/n,backward_attempt_share=len(subsets['backward'])/n,
        lateral_attempt_share=len(subsets['lateral'])/n,progressive_attempt_share=len(subsets['progressive'])/n,
        long_pass_attempt_share=len(subsets['long_pass'])/n,switch_attempt_share=len(subsets['switch'])/n,
        forward_completion_rate=_rate(subsets['forward']),progressive_completion_rate=_rate(subsets['progressive']),
        long_pass_completion_rate=_rate(subsets['long_pass']),switch_completion_rate=_rate(subsets['switch']),
        final_third_entry_attempts=len(subsets['final_third_entry']),
        final_third_entry_completions=sum(row['completed'] for row in subsets['final_third_entry']),
        final_third_entry_completion_rate=_rate(subsets['final_third_entry']),box_entry_attempts=len(subsets['box_entry']),
        box_entry_completions=sum(row['completed'] for row in subsets['box_entry']),
        box_entry_completion_rate=_rate(subsets['box_entry']),pass_map_key_passes=sum(row['key_pass'] for row in passes),
        dominant_origin_zone=Counter(origins).most_common(1)[0][0],
        dominant_origin_zone_number=Counter(origin_numbers).most_common(1)[0][0],
        dominant_destination_zone=Counter(destinations).most_common(1)[0][0],
        dominant_destination_zone_number=Counter(destination_numbers).most_common(1)[0][0],
        origin_zone_entropy=_zone_entropy(origin_numbers),destination_zone_entropy=_zone_entropy(destination_numbers),
    )
    transitions=[]
    for (origin_number,destination_number),rows in _group_passes(passes).items():
        done=sum(row['completed'] for row in rows)
        transitions.append({'origin_zone':rows[0]['origin_zone'],'origin_zone_number':origin_number,
                            'destination_zone':rows[0]['destination_zone'],
                            'destination_zone_number':destination_number,'attempts':len(rows),
                            'completed':done,'completion_rate':done/len(rows)})
    return result,passes,transitions


def _group_passes(passes):
    grouped={}
    for row in passes:
        grouped.setdefault((row['origin_zone_number'],row['destination_zone_number']),[]).append(row)
    return grouped


def _dribble_record(index,event):
    point=_point(event.get('playerCoordinates'))
    if not point:return None
    x,y=point;distance=math.hypot(105-x*1.05,34-y*.68);maximum=math.hypot(105,34)
    return {
        'dribble_index':index,'x_raw':x,'y_raw':y,'forwardness':_forward(x),
        'signed_width':_signed_width(y),'absolute_width':abs(_signed_width(y)),
        'zone':f'{_third(x)}_{_channel(y)}','zone_number':zone_number(x,y),
        'successful':event.get('outcome') is True,
        'defensive_third':x<100/3,'middle_third':100/3<=x<200/3,'final_third':x>=200/3,
        'central':RIGHT_HALF_SPACE_MAX<=y<LEFT_HALF_SPACE_MIN,
        'wide':y<RIGHT_WING_MAX or y>=LEFT_WING_MIN,'opposition_box':_in_box(x,y),
        'goal_proximity':1-distance/maximum,'event_action_type':event.get('eventActionType'),
    }


def _mean(rows,key):
    return sum(row[key] for row in rows)/len(rows) if rows else None


def extract_dribble_features(match,appearance,directory,heat_features):
    """Return a versioned dribble summary and one normalized row per attempt."""
    files=match.metadata.get('sofascore_spatial_files',{}).get(str(appearance.player.id),{})
    status=match.metadata.get('sofascore_spatial_status',{}).get(str(appearance.player.id),{}).get('rating_breakdown','unavailable')
    result={name:None for name in DRIBBLE_FEATURE_COLUMNS}
    result.update(dribble_feature_version=DRIBBLE_FEATURE_VERSION,dribble_map_status=status)
    published=appearance.stats.actions.get('dribbles')
    if published:
        result['published_dribble_attempts']=published.attempts
        result['published_dribbles_completed']=published.completions
    payload,source=action_payload(match,appearance,directory,'dribbles')
    if not isinstance(payload,dict) or not isinstance(payload.get('dribbles'),list):
        if status=='complete':result['dribble_map_status']='schema_unavailable'
        return result,[]
    result.update(dribble_map_source=source,dribble_attempts=0,mapped_dribbles_completed=0,
                  successful_dribble_danger=0.0,failed_dribble_danger=0.0)
    dribbles=[row for index,event in enumerate(payload['dribbles']) if (row:=_dribble_record(index,event))]
    result['dribble_map_status']='partial' if payload.get('_rejected') or len(dribbles)!=len(payload['dribbles']) else 'complete';n=len(dribbles);successful=[row for row in dribbles if row['successful']]
    result['dribble_attempts']=n;result['mapped_dribbles_completed']=len(successful)
    if not published or published.attempts is None or published.completions is None:
        result['dribble_reconciliation_status']='published_counts_unavailable'
    elif published.attempts==n and published.completions==len(successful):
        result['dribble_reconciliation_status']='matched'
    else:
        result['dribble_reconciliation_status']='mismatch'
    if not dribbles:return result,dribbles
    failed=[row for row in dribbles if not row['successful']];minutes=appearance.minutes_played
    touches=heat_features.get('touch_count')
    zones=[row['zone'] for row in dribbles];zone_numbers=[row['zone_number'] for row in dribbles]
    mf=_mean(dribbles,'forwardness');mw=_mean(dribbles,'signed_width')
    variability=math.sqrt(sum((row['forwardness']-mf)**2+(row['signed_width']-mw)**2 for row in dribbles)/n)
    subset=lambda key:[row for row in dribbles if row[key]]
    danger_total=sum(row['goal_proximity'] for row in dribbles)
    result.update(
        dribble_completion_rate=len(successful)/n,
        dribble_attempts_per_90=n*90/minutes if minutes and minutes>0 else None,
        dribbles_completed_per_90=len(successful)*90/minutes if minutes and minutes>0 else None,
        dribble_attempts_per_100_touches=n*100/touches if touches else None,
        dribbles_completed_per_100_touches=len(successful)*100/touches if touches else None,
        average_dribble_forwardness=mf,average_dribble_signed_width=mw,
        average_dribble_absolute_width=_mean(dribbles,'absolute_width'),dribble_location_variability=variability,
        dribble_location_entropy=_zone_entropy(zone_numbers),dominant_dribble_zone=Counter(zones).most_common(1)[0][0],
        dominant_dribble_zone_number=Counter(zone_numbers).most_common(1)[0][0],
        defensive_third_attempt_share=len(subset('defensive_third'))/n,middle_third_attempt_share=len(subset('middle_third'))/n,
        final_third_attempt_share=len(subset('final_third'))/n,central_attempt_share=len(subset('central'))/n,
        wide_attempt_share=len(subset('wide'))/n,box_attempt_share=len(subset('opposition_box'))/n,
        final_third_completion_rate=_rate(subset('final_third'),'successful'),central_completion_rate=_rate(subset('central'),'successful'),
        wide_completion_rate=_rate(subset('wide'),'successful'),box_completion_rate=_rate(subset('opposition_box'),'successful'),
        successful_dribble_forwardness=_mean(successful,'forwardness'),
        successful_dribble_signed_width=_mean(successful,'signed_width'),failed_dribble_forwardness=_mean(failed,'forwardness'),
        failed_dribble_signed_width=_mean(failed,'signed_width'),average_dribble_goal_proximity=_mean(dribbles,'goal_proximity'),
        successful_dribble_danger=sum(row['goal_proximity'] for row in successful),
        failed_dribble_danger=sum(row['goal_proximity'] for row in failed),
        danger_weighted_completion_rate=(sum(row['goal_proximity'] for row in successful)/danger_total if danger_total else None),
    )
    touch_forward=heat_features.get('average_forwardness');touch_width=heat_features.get('average_absolute_width')
    touch_signed=heat_features.get('average_signed_width')
    if touch_forward is not None and touch_signed is not None:
        result['dribble_centroid_distance_from_touch_centroid']=math.hypot(mf-touch_forward,mw-touch_signed)
        result['dribble_forwardness_minus_touch_forwardness']=mf-touch_forward
    if touch_width is not None:result['dribble_width_minus_touch_width']=result['average_dribble_absolute_width']-touch_width
    return result,dribbles

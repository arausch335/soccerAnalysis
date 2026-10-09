"""Aggregate observed event actions and 15-region distributions without joining on names."""
import numpy as np
import pandas as pd
from pitch_zones import (LEFT_HALF_SPACE_MIN, LEFT_WING_MIN, RIGHT_HALF_SPACE_MAX,
                         RIGHT_WING_MAX)
from .config import ZONES,THIRDS,CHANNELS,FAMILIES,SHOT_TYPES,DEFENSE_TYPES,ON_BALL_TYPES

USECOLS=['match_id','player_id','team_id','period','event_uid','event_index','event_type','x','y','end_x','end_y',
 'is_administrative','is_touch','open_play_or_restart','is_progressive','is_final_third_entry','is_box_entry',
 'inferred_receiver_id','receiver_confidence','inferred_carry_distance_m','calculated_length_m','dx_m','dy_m',
 'goal_distance_reduction_m','start_zone','end_zone','qualifier_names']

def zone_codes(x,y):
    valid=x.between(0,100)&y.between(0,100)
    a=np.minimum((x.fillna(0)/ (100/3)).astype(int),2)
    clean=y.fillna(0)
    b=np.select(
        [clean.lt(RIGHT_WING_MAX),clean.lt(RIGHT_HALF_SPACE_MAX),
         clean.lt(LEFT_HALF_SPACE_MIN),clean.lt(LEFT_WING_MIN)],
        [0,1,2,3],default=4)
    return pd.Series(np.where(valid,a*5+b,-1),index=x.index)

def aggregate_events(path):
    e=pd.read_csv(path,usecols=USECOLS,low_memory=False)
    if e.duplicated(['match_id','event_index']).any():raise ValueError('Duplicate source events')
    raw_count=len(e);e=e[e.period.isin([1,2,3,4])].copy()
    audit=[]
    for mid,g in e.groupby('match_id',sort=False):
        periods=set(g.period);half_ends=set(g.loc[g.event_type.eq('End'),'period'])
        audit.append({'match_id':mid,'source_events':len(g),'has_both_halves':{1,2}.issubset(periods),
          'has_both_half_ends':{1,2}.issubset(half_ends),'event_stream_usable':{1,2}.issubset(periods)&{1,2}.issubset(half_ends)})
    usable={a['match_id'] for a in audit if a['event_stream_usable']}
    player=e[e.player_id.notna()&e.match_id.isin(usable)].copy();player['provider_player_id']=player.player_id.astype('int64')
    keys=['match_id','provider_player_id'];ix=pd.MultiIndex.from_frame(player[keys].drop_duplicates())
    out={};specs=[]
    def count(name,mask,group='event context',candidate=False,label=None):
        out[name]=player.loc[mask].groupby(keys).size().reindex(ix,fill_value=0)
        specs.append(dict(name=name,kind='count',group=group,candidate=candidate,label=label or name.removeprefix('ev_').replace('_',' ').capitalize()+' / 90',source='whoscored events'))
    def total(name,values):out[name]=values.groupby([player[k] for k in keys]).sum(min_count=1).reindex(ix)
    passes=player.event_type.isin(['Pass','OffsidePass']);op=player.open_play_or_restart.eq('open_play')
    geom=player.x.between(0,100)&player.y.between(0,100)&player.end_x.between(0,100)&player.end_y.between(0,100)
    valid=player.x.between(0,100)&player.y.between(0,100)
    count('ev_actions',~player.is_administrative.fillna(True).astype(bool))
    count('ev_passes',passes);count('ev_open_play_passes',passes&op)
    count('ev_geometry_passes',passes&op&geom)
    count('ev_progressive_passes',passes&op&geom&player.is_progressive.fillna(False).astype(bool),'passing')
    count('ev_forward_passes',passes&op&geom&player.dx_m.gt(5),'passing')
    count('ev_switch_passes',passes&op&geom&player.dy_m.abs().ge(35),'passing')
    count('ev_final_third_entries',passes&op&geom&player.is_final_third_entry.fillna(False).astype(bool),'passing')
    count('ev_box_entries',passes&op&geom&player.is_box_entry.fillna(False).astype(bool),'passing')
    count('ev_take_ons',player.event_type.eq('TakeOn'),'carrying')
    count('ev_shots',player.event_type.isin(SHOT_TYPES),'attacking')
    count('ev_box_actions',player.event_type.isin(ON_BALL_TYPES)&op&valid&player.x.ge(84.3)&player.y.between(20.35,79.65),'attacking',True,'Open-play box actions / 90')
    count('ev_defensive_actions',player.event_type.isin(DEFENSE_TYPES),'defending')
    count('ev_carries_inferred',player.inferred_carry_distance_m.notna(),'inferred movement')
    total('ev_carry_distance_inferred_m',player.inferred_carry_distance_m.fillna(0))
    specs.append(dict(name='ev_carry_distance_inferred_m',kind='count',group='inferred movement',candidate=False,label='Inferred carry distance (m / 90)',source='event-layer heuristic'))
    for q,col in [('CornerTaken','ev_corners'),('FreekickTaken','ev_free_kicks'),('ThrowIn','ev_throw_ins'),('GoalKick','ev_goal_kicks')]:
        count(col,passes&player.qualifier_names.str.contains('"'+q+'"',regex=False,na=False),'set pieces',True)
    total('ev_pass_length_sum_m',player.calculated_length_m.where(passes&op&geom,0))
    total('ev_pass_forward_sum_m',player.dx_m.where(passes&op&geom,0))
    for name,numerator,denom,label in [
      ('ev_progressive_pass_share','ev_progressive_passes','ev_geometry_passes','Progressive pass attempt share'),
      ('ev_forward_pass_share','ev_forward_passes','ev_geometry_passes','Forward pass attempt share'),
      ('ev_switch_pass_share','ev_switch_passes','ev_geometry_passes','Switch attempt share'),
      ('ev_final_third_entry_share','ev_final_third_entries','ev_geometry_passes','Final-third entry attempt share'),
      ('ev_box_entry_share','ev_box_entries','ev_geometry_passes','Box-entry attempt share'),
      ('ev_pass_length_mean_m','ev_pass_length_sum_m','ev_geometry_passes','Mean open-play pass length (m)'),
      ('ev_pass_forward_mean_m','ev_pass_forward_sum_m','ev_geometry_passes','Mean open-play pass forward gain (m)')]:
        specs.append(dict(name=name,kind='ratio',numerator=numerator,denominator=denom,group='passing',candidate=True,label=label,source='whoscored events'))
    pzone=zone_codes(player.x,player.y);ezone=zone_codes(player.end_x,player.end_y)
    family_masks={
      'on_ball':player.event_type.isin(ON_BALL_TYPES)&op,
      'pass_origin':passes,'pass_destination':passes,'take_on':player.event_type.eq('TakeOn'),
      'shot':player.event_type.isin(SHOT_TYPES),'defensive':player.event_type.isin(DEFENSE_TYPES),
      'recovery':player.event_type.eq('BallRecovery')}
    zones=[]
    for family,mask in family_masks.items():
        codes=ezone if family=='pass_destination' else pzone
        z=player.loc[mask&codes.ge(0),keys].copy();z['zone_index']=codes.loc[z.index]
        z=z.groupby(keys+['zone_index']).size().rename('count').reset_index();z['family']=family;zones.append(z)
    # Pass-end reception is inferred; it is not a provider-recorded ball receipt.
    rec=e[e.match_id.isin(usable)&e.inferred_receiver_id.notna()&e.receiver_confidence.isin(['high','medium'])].copy()
    rec['provider_player_id']=rec.inferred_receiver_id.astype('int64');rec['zone_index']=zone_codes(rec.end_x,rec.end_y)
    rec=rec[rec.zone_index.ge(0)].groupby(keys+['zone_index']).size().rename('count').reset_index();rec['family']='reception_inferred';zones.append(rec)
    zones=pd.concat(zones,ignore_index=True)
    for family in FAMILIES:
        z=zones[zones.family.eq(family)].pivot_table(index=keys,columns='zone_index',values='count',fill_value=0)
        z=z.reindex(index=ix,columns=range(15),fill_value=0).fillna(0)
        out['zone_'+family+'_n']=z.sum(axis=1)
        for zi,zone in enumerate(ZONES):out['zone_'+family+'_'+zone]=z[zi]
    for third in THIRDS:
        for band,channels in [('wide',['right_wing','left_wing']),('half_space',['right_half_space','left_half_space']),('center',['center'])]:
            name='region_'+third+'_'+band
            num='sum_'+name;out[num]=sum(out['zone_on_ball_'+third+'_'+c] for c in channels)
            specs.append(dict(name=name,kind='ratio',numerator=num,denominator='zone_on_ball_n',group='field regions',candidate=True,label=third.title()+' '+band.replace('_',' ')+' share',source='open-play on-ball events'))
    for family in ['take_on','shot','defensive','recovery','reception_inferred']:
        for band,cols in [('attacking',[z for z in ZONES if z.startswith('attacking_')]),('wide',[z for z in ZONES if z.endswith('_wing')])]:
            name='ev_'+family+'_'+band+'_share';num='sum_'+name;out[num]=sum(out['zone_'+family+'_'+z] for z in cols)
            specs.append(dict(name=name,kind='ratio',numerator=num,denominator='zone_'+family+'_n',group='action locations' if family!='reception_inferred' else 'inferred movement',candidate=family!='reception_inferred',label=family.replace('_',' ').title()+' '+band+' share',source='whoscored events'))
    zone_n=out['zone_on_ball_n'].replace(0,np.nan)
    # Raw moments support correct event-weighted season means and dispersion.
    spatial=family_masks['on_ball']&valid
    for axis in ['x','y']:
        total('ev_on_ball_'+axis+'_sum',player[axis].where(spatial,0))
        specs.append(dict(name='ev_mean_'+axis,kind='ratio',numerator='ev_on_ball_'+axis+'_sum',denominator='zone_on_ball_n',group='field position context',candidate=False,label='Mean on-ball '+axis+' (0–100)',source='whoscored events'))
    team=player[passes&op].groupby(['match_id','team_id']).size().rename('team_open_play_passes').reset_index()
    return pd.DataFrame(out,index=ix).reset_index(),zones,pd.DataFrame(audit),specs,team,{'raw_event_rows':raw_count,'in_play_period_rows':len(e),'usable_event_matches':len(usable)}

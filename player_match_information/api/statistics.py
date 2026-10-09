"""Provider field mappings and conservative per-match synthesis.

No inferred zeros for sparse fields. Whole action pairs have one provider.
"""
from copy import deepcopy

SCALARS = {
 'sofascore_rating':('rating','rating',None,None,'rating'),
 'sofascore_keeper_save_value':('goalkeeping','keeperSaveValue',None,None,'provider value'),
 'sofascore_total_progression':('carrying','totalProgression',None,None,'provider value'),
 'sofascore_best_ball_carry_progression':('carrying','bestBallCarryProgression',None,None,'provider value'),
 'penalties_faced':('goalkeeping','penaltyFaced',None,None,'count'),
 'crosses_not_claimed':('goalkeeping','crossNotClaimed',None,None,'count'),
 'fotmob_rating':('rating',None,'rating_title',None,'rating'),
 'fotmob_fantasy_points':('fantasy',None,'fantasy_points',None,'points'),
 'fotmob_fantasy_bonus':('fantasy',None,'fantasy_points',None,'points'),
 'fotmob_xg_plus_xa':('shooting',None,'xg_and_xa',None,'expected goals and assists'),
 'fotmob_shot_accuracy':('shooting',None,'shot_accuracy',None,'fraction'),
 'fotmob_defensive_actions':('defending',None,'defensive_actions',None,'count'),
 'corners_taken':('passing',None,'corners',None,'count'),
 'own_goals':('defending','ownGoals','owngoal',None,'count'),
 'penalties_missed':('shooting','penaltyMiss','missed_penalty',None,'count'),
 'penalties_saved':('goalkeeping','penaltySave','saved_penalties',None,'count'),
 'diving_saves':('goalkeeping',None,'keeper_diving_save',None,'count'),
 'keeper_sweeper_actions':('goalkeeping',None,'keeper_sweeper',None,'count'),
 'keeper_throws':('goalkeeping',None,'player_throws',None,'count'),
 'fotmob_walking_distance_m':('pace',None,'physical_metrics_walking',None,'metres'),
 'fotmob_jogging_distance_m':('pace',None,'physical_metrics_jogging',None,'metres'),
 'total_duels_lost':('physical','duelLost','duel_lost',None,'count'),
 'minutes_played':('context','minutesPlayed','minutes_played',None,'minutes'),
 'goals':('shooting','goals','goals',None,'count'),
 'assists':('passing','goalAssist','assists',None,'count'),
 'touches':('involvement','touches','touches','Touches','count'),
 'touches_opposition_box':('involvement',None,'touches_opp_box',None,'count'),
 'key_passes':('passing','keyPass','chances_created','KeyPassTotal','count'),
 'passes_into_final_third':('passing',None,'passes_into_final_third',None,'count'),
 'line_breaking_passes':('passing',None,'line_breaking_passes',None,'count'),
 'big_chances_created':('passing','bigChanceCreated','big_chance_created_team_title',None,'count'),
 'shots':('shooting','totalShots','total_shots','ShotsTotal','count'),
 'shots_on_target':('shooting','onTargetScoringAttempt','ShotsOnTarget','ShotOnTarget','count'),
 'shots_off_target':('shooting','shotOffTarget','ShotsOffTarget',None,'count'),
 'shots_blocked':('shooting','blockedScoringAttempt','blocked_shots',None,'count'),
 'woodwork':('shooting','hitWoodwork','shots_woodwork',None,'count'),
 'big_chances_missed':('shooting','bigChanceMissed','big_chance_missed_title',None,'count'),
 'xg':('shooting','expectedGoals','expected_goals',None,'expected goals'),
 'xgot':('shooting','expectedGoalsOnTarget','expected_goals_on_target_variant',None,'expected goals'),
 'xa':('passing','expectedAssists','expected_assists',None,'expected assists'),
 'non_penalty_xg':('shooting',None,'expected_goals_non_penalty',None,'expected goals'),
 'dispossessed':('dribbling','dispossessed','dispossessed','Dispossessed','count'),
 'unsuccessful_touches':('dribbling','unsuccessfulTouch',None,'Turnover','count'),
 'possession_lost':('dribbling','possessionLostCtrl',None,None,'count'),
 'fouls_suffered':('physical','wasFouled','was_fouled','FoulGiven','count'),
 'fouls_committed':('defending','fouls','fouls','FoulCommitted','count'),
 'offsides':('involvement','totalOffside','Offsides','OffsideGiven','count'),
 'tackles':('defending','totalTackle','matchstats.headers.tackles','TackleWonTotal','count'),
 'tackles_won_sofascore':('defending','wonTackle',None,None,'count'),
 'interceptions':('defending','interceptionWon','interceptions','InterceptionAll','count'),
 'clearances':('defending','totalClearance','clearances','ClearanceTotal','count'),
 'headed_clearances':('defending',None,'headed_clearance',None,'count'),
 'defensive_shot_blocks':('defending','outfielderBlock','shot_blocks','ShotBlocked','count'),
 'recoveries':('defending','ballRecovery','recoveries',None,'count'),
 'dribbled_past':('defending','challengeLost','dribbled_past',None,'count'),
 'last_man_tackles':('defending','lastManTackle','last_man_tackle',None,'count'),
 'goal_line_clearances':('defending','clearanceOffLine','clearance_off_the_line',None,'count'),
 'errors_leading_to_shot':('defending','errorLeadToAShot',None,None,'count'),
 'errors_leading_to_goal':('defending','errorLeadToAGoal','errors_led_to_goal',None,'count'),
 'penalties_won':('involvement','penaltyWon','penalties_won',None,'count'),
 'penalties_conceded':('defending','penaltyConceded','conceded_penalties',None,'count'),
 'top_speed_kmh':('pace','topSpeed','physical_metrics_topspeed',None,'km/h'),
 'distance_covered_m':('pace','kilometersCovered','physical_metrics_distance_covered',None,'metres'),
 'running_distance_m':('pace','metersCoveredRunningKm',None,None,'metres'),
 'fotmob_running_distance_m':('pace',None,'physical_metrics_running',None,'metres'),
 'high_speed_running_distance_m':('pace','metersCoveredHighSpeedRunningKm',None,None,'metres'),
 'sprinting_distance_m':('pace','metersCoveredSprintingKm','physical_metrics_sprinting',None,'metres'),
 'sprints':('pace','numberOfSprints','physical_metrics_number_of_sprints',None,'count'),
 'ball_carries':('carrying','ballCarriesCount',None,None,'count'),
 'carry_distance_m':('carrying','totalBallCarriesDistance',None,None,'metres'),
 'progressive_carries':('carrying','progressiveBallCarriesCount',None,None,'count'),
 'progressive_carry_distance_m':('carrying','totalProgressiveBallCarriesDistance',None,None,'metres'),
 'saves':('goalkeeping','saves','saves',None,'count'),
 'saves_inside_box':('goalkeeping','savedShotsFromInsideTheBox','saves_inside_box',None,'count'),
 'goals_prevented':('goalkeeping','goalsPrevented','goals_prevented',None,'expected goals'),
 'xgot_faced':('goalkeeping',None,'expected_goals_on_target_faced',None,'expected goals'),
 'goals_conceded_goalkeeper':('goalkeeping',None,'goals_conceded',None,'count'),
 'high_claims':('goalkeeping','goodHighClaim','keeper_high_claim',None,'count'),
 'punches':('goalkeeping','punches','punches',None,'count'),
}

PAIRS = {
 'keeper_sweeper_actions':('goalkeeping',('totalKeeperSweeper','accurateKeeperSweeper'),None,(None,None)),
 'passes':('passing',('totalPass','accuratePass'),'accurate_passes',('TotalPasses',None)),
 'long_balls':('passing',('totalLongBalls','accurateLongBalls'),'long_balls_accurate',('PassLongBallTotal','PassLongBallAccurate')),
 'crosses':('passing',('totalCross','accurateCross'),'accurate_crosses',('PassCrossTotal','PassCrossAccurate')),
 'through_balls':('passing',(None,None),None,('PassThroughBallTotal','PassThroughBallAccurate')),
 'dribbles':('dribbling',('totalContest','wonContest'),'dribbles_succeeded',(None,'DribbleWon')),
 'ground_duels':('physical',(None,None),'ground_duels_won',(None,None)),
 'total_duels':('physical',('duelLost+duelWon','duelWon'),'duel_won',(None,None)),
 'aerial_duels':('physical',('aerialLost+aerialWon','aerialWon'),'aerials_won',(None,'DuelAerialWon')),
 'own_half_passes':('passing',('totalOwnHalfPasses','accurateOwnHalfPasses'),None,(None,None)),
 'opposition_half_passes':('passing',('totalOppositionHalfPasses','accurateOppositionHalfPasses'),None,(None,None)),
}

def number(value):
    if value is None or isinstance(value,bool): return None
    if isinstance(value,(int,float)):
        import math
        if not math.isfinite(value): raise ValueError('Non-finite statistic')
        return int(value) if float(value).is_integer() else value
    return None


def sofa_stats(raw):
    metrics={}
    for metric,(_,key,_,_,_) in SCALARS.items():
        value=number(raw.get(key))
        if metric in ('distance_covered_m','running_distance_m','high_speed_running_distance_m','sprinting_distance_m') and value is not None:value*=1000
        metrics[metric]=value
    for stem,(_,keys,_,_) in PAIRS.items():
        for suffix,key in zip(('attempted','completed'),keys):
            if key and '+' in key:
                values=[number(raw.get(k)) for k in key.split('+')]
                value=sum(values) if all(v is not None for v in values) else None
            else:value=number(raw.get(key))
            metrics[stem+'_'+suffix]=value
    return metrics


def fotmob_stats(player):
    raw={}
    for group in player.get('stats',[]):
        for item in group.get('stats',{}).values():
            key=item.get('key')
            if key:
                if key in raw and raw[key]!=item['stat']:raise ValueError('Conflicting FotMob statistic groups')
                raw[key]=item['stat']
    metrics={metric:number(raw.get(key,{}).get('value')) for metric,(_,_,key,_,_) in SCALARS.items()}
    for stem,(_,_,key,_) in PAIRS.items():
        metrics[stem+'_attempted']=number(raw.get(key,{}).get('total'))
        metrics[stem+'_completed']=number(raw.get(key,{}).get('value'))
    won=number(raw.get('duel_won',{}).get('value'))
    lost=number(raw.get('duel_lost',{}).get('value'))
    # Missing losses are unknown, not zero. Keep an explicit partial pair.
    metrics['total_duels_attempted']=won+lost if won is not None and lost is not None else None
    metrics['fotmob_fantasy_bonus']=number(raw.get('fantasy_points',{}).get('bonus'))
    accuracy=raw.get('shot_accuracy',{})
    numerator=number(accuracy.get('value'));denominator=number(accuracy.get('total'))
    metrics['fotmob_shot_accuracy']=(numerator/denominator
        if numerator is not None and denominator is not None and denominator>0 else None)
    return raw,metrics


def synthesize(sofa_raw, providers, appeared, standalone=False):
    from models import AttemptCompletion
    available={} if standalone else {'sofascore':sofa_stats(sofa_raw)}
    available.update({provider:entry['metrics'] for provider,entry in providers.items()})
    order=[p for p in ('sofascore','fotmob','whoscored') if p in available]
    metrics={};sources={};actions={};differences=[]
    names=set(SCALARS)|{'shots_inside_box','shots_outside_box'}
    for name in sorted(names):
        candidates=[(p,available[p].get(name)) for p in order if available[p].get(name) is not None]
        source,value=candidates[0] if appeared and candidates else (None,None)
        metrics[name]=value;sources[name]=source
    for stem in PAIRS:
        keys=(stem+'_attempted',stem+'_completed')
        full=[p for p in order if all(available[p].get(k) is not None for k in keys)]
        partial=[p for p in order if any(available[p].get(k) is not None for k in keys)]
        source=(full or partial or [None])[0] if appeared else None
        a,c=(available[source].get(k) for k in keys) if source else (None,None)
        action=AttemptCompletion(a,c)
        if appeared:actions[stem]=action
        for key,value in zip(keys,(a,c)):
            metrics[key]=value;sources[key]=source if value is not None else None
    # Preserve comparable disagreements rather than averaging providers.
    for name,value in metrics.items():
        if value is None: continue
        for provider in order:
            other=available[provider].get(name)
            if provider!=sources[name] and other is not None and abs(other-value)>1e-6:
                differences.append({'metric':name,'selected_provider':sources[name],'selected_value':value,
                                    'other_provider':provider,'other_value':other})
    return {'metrics':metrics,'sources':sources,'actions':actions,'differences':differences}

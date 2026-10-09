"""Explicit role-only feature contract; outcomes remain separate context."""
from dataclasses import dataclass

@dataclass
class Config:
    min_minutes: int = 450
    min_matches: int = 5
    min_event_coverage: float = .8
    min_feature_coverage: float = .8
    min_position_share: float = .6
    min_cohort: int = 16
    bootstrap_runs: int = 20
    seed: int = 42

POSITION_MAP={
    'GK':'Goalkeeper','DC':'Centre-back','DR':'Full-back / wing-back','DL':'Full-back / wing-back',
    'DMR':'Full-back / wing-back','DML':'Full-back / wing-back',
    'DMC':'Central midfield','MC':'Central midfield','AMC':'Attacking midfield',
    'AMR':'Winger / wide midfield','AML':'Winger / wide midfield','FWR':'Winger / wide midfield',
    'FWL':'Winger / wide midfield','MR':'Winger / wide midfield','ML':'Winger / wide midfield','FW':'Striker'}
THIRDS=['defensive','middle','attacking']
CHANNELS=['right_wing','right_half_space','center','left_half_space','left_wing']
ZONES=[a+'_'+b for a in THIRDS for b in CHANNELS]
FAMILIES=['on_ball','pass_origin','pass_destination','take_on','shot','defensive','recovery','reception_inferred']
SHOT_TYPES={'Goal','MissedShots','SavedShot','ShotOnPost'}
DEFENSE_TYPES={'Tackle','Challenge','Interception','Clearance','BlockedPass'}
ON_BALL_TYPES={'Pass','OffsidePass','TakeOn','BallTouch','Dispossessed',*SHOT_TYPES}
# Registry entries: source column, aggregation (count/mean/ratio), group, denominator, role model candidate.
BASE_CANDIDATES={
 'passes_attempted':'involvement','touches':'involvement','dribbles_attempted':'carrying',
 'shots':'attacking','shots_inside_box':'attacking','shots_outside_box':'attacking',
 'offsides':'attacking','tackles':'defending','interceptions':'defending',
 'clearances':'defending','headed_clearances':'defending','defensive_shot_blocks':'defending',
 'recoveries':'defending','aerial_duels_attempted':'duels','fouls_suffered':'duels',
 'keeper_sweeper_actions':'goalkeeping','keeper_throws':'goalkeeping','high_claims':'goalkeeping','punches':'goalkeeping'}
# Not model inputs: adverse outcomes, redundant totals, unverified running units,
# inferred movement, execution success, or source-reported missing metrics.
EXTRA_CONTEXT=['distance_covered_m','high_speed_running_distance_m','running_distance_m',
 'sprinting_distance_m','sprints','fotmob_jogging_distance_m','fotmob_running_distance_m','fotmob_walking_distance_m']
HEAT_CONTEXT=['average_forwardness','average_signed_width','average_absolute_width','forward_variability',
 'lateral_variability','radial_dispersion','spatial_entropy','occupied_area_share','defensive_third_share',
 'middle_third_share','final_third_share','left_wing_share','left_half_space_share','central_share',
 'right_half_space_share','right_wing_share','opposition_box_share']

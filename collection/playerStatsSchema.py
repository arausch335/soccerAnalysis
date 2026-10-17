"""Stable player-match schema shared by the normal and FotMob-only exports."""
from providers.statistics import SCALARS, PAIRS
from .spatial import MINIMAL_SPATIAL_COLUMNS

CONTEXT_COLUMNS = (
    'match_id','date','season','competition','team_id','team','opponent_id','opponent','player_id',
    'player_name','starter','position','minutes_played',
)
PLAYER_MATCH_COLUMNS = tuple(dict.fromkeys((
    *CONTEXT_COLUMNS, *sorted(set(SCALARS) | {'shots_inside_box','shots_outside_box'}),
    *(stem+'_'+suffix for stem in PAIRS for suffix in ('attempted','completed')),
    'appeared','fotmob_player_stats_status','shirt_number','fotmob_opta_id',
    'fotmob_position_id','fotmob_age_reported','fotmob_country','fotmob_market_value',
    *(stem+'_completion_rate' for stem in PAIRS), *MINIMAL_SPATIAL_COLUMNS,
)))

# Capability is structural: columns never disappear just because one match has
# no value. Unsupported Sofascore/WhoScored measurements remain blank.
FOTMOB_COLUMNS = {
    'match_id','date','team_id','team','opponent_id','opponent','player_id','player_name','starter',
    'position','minutes_played','appeared','shirt_number',
    *(name for name,(_,_,key,_,_) in SCALARS.items() if key is not None),
    *(stem+'_'+suffix for stem,(_,_,key,_) in PAIRS.items() if key is not None
      for suffix in ('attempted','completed','completion_rate')),
    'shots_inside_box','shots_outside_box',
    'fotmob_player_stats_status','fotmob_opta_id','fotmob_position_id',
    'fotmob_age_reported','fotmob_country','fotmob_market_value',
}


def fotmob_column(name):
    if name not in FOTMOB_COLUMNS:
        return name
    return 'fmb_' + name.removeprefix('fotmob_')


FOTMOB_PLAYER_MATCH_COLUMNS = tuple(fotmob_column(c) for c in PLAYER_MATCH_COLUMNS)

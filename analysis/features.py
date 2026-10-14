identifiers = [
    "match_id",
    "date",
    "season",
    "team",
    "opponent",
    "player_id",
    "player_name",
]

gk_features = [
    'saves_inside_box',
    'saves',
    'high_claims',
    'keeper_sweeper_actions',
    'keeper_throws',
    'xgot_faced',
    'diving_saves',
    'goals_prevented',
    'goals_conceded_goalkeeper',
    'punches',
    'penalties_saved'
]

missing_features = [
    "progressive_carry_distance_m",
    "progressive_carries"
]

ROLE = {

    # -------------------------
    # PASSING / CREATION
    # -------------------------
    "passing": [
        "passes_attempted",
        "long_balls_attempted",
        "crosses_attempted",
        "through_balls_attempted",
        "own_half_passes_attempted",
        "opposition_half_passes_attempted",
        "corners_taken",
        "passes_into_final_third",
        "line_breaking_passes",
    ],

    # -------------------------
    # DRIBBLING / CARRYING
    # -------------------------
    "dribbling": [
        "ball_carries",
        "carry_distance_m",
        "progressive_carries",
        "progressive_carry_distance_m",
        "dribbles_attempted",
    ],

    # -------------------------
    # SHOOTING / ATTACKING
    # -------------------------
    "shooting": [
        "shots",
        "shots_inside_box",
        "shots_outside_box",
        "touches_opposition_box",
        "offsides",
    ],

    # -------------------------
    # DEFENSIVE
    # -------------------------
    "defense": [
        "tackles",
        "interceptions",
        "clearances",
        "headed_clearances",
        "defensive_shot_blocks",
        "recoveries",
        "fotmob_defensive_actions",
    ],

    # -------------------------
    # DUELS / PHYSICAL
    # -------------------------
    "duels": [
        "ground_duels_attempted",
        "total_duels_attempted",
        "aerial_duels_attempted",
        "fouls_committed",
        "fouls_suffered",
    ],

    # -------------------------
    # GENERAL INVOLVEMENT
    # -------------------------
    "involvement": [
        "touches",
        "possession_lost",
    ],

    # -------------------------
    # GOALKEEPING ROLE
    # -------------------------
    "goalkeeping": [
        "keeper_sweeper_actions",
        "keeper_throws",
        "high_claims",
        "punches",
        "xgot_faced",
    ],
}

PERFORMANCE = {

    # -------------------------
    # PASSING / CREATION
    # -------------------------
    "passing": [
        "passes_completed",
        "passes_completion_rate",

        "long_balls_completed",
        "long_balls_completion_rate",

        "crosses_completed",
        "crosses_completion_rate",

        "through_balls_completed",
        "through_balls_completion_rate",

        "own_half_passes_completed",
        "own_half_passes_completion_rate",

        "opposition_half_passes_completed",
        "opposition_half_passes_completion_rate",

        "key_passes",
        "assists",
        "big_chances_created",
        "xa",
    ],

    # -------------------------
    # DRIBBLING / BALL CONTROL
    # -------------------------
    "dribbling": [
        "dribbles_completed",
        "dribbles_completion_rate",
        "dispossessed",
        "unsuccessful_touches",
    ],

    # -------------------------
    # SHOOTING / ATTACKING
    # -------------------------
    "shooting": [
        "goals",
        "xg",
        "non_penalty_xg",
        "xgot",
        "fotmob_xg_plus_xa",

        "shots_on_target",
        "shots_off_target",
        "shots_blocked",
        "fotmob_shot_accuracy",

        "big_chances_missed",
        "woodwork",

        "penalties_won",
        "penalties_missed",
    ],

    # -------------------------
    # DEFENSIVE
    # -------------------------
    "defense": [
        "tackles_won_sofascore",
        "dribbled_past",
        "last_man_tackles",
        "goal_line_clearances",

        "errors_leading_to_shot",
        "errors_leading_to_goal",

        "penalties_conceded",
        "own_goals",
    ],

    # -------------------------
    # DUELS / PHYSICAL CONTESTS
    # -------------------------
    "duels": [
        "ground_duels_completed",
        "ground_duels_completion_rate",

        "total_duels_completed",
        "total_duels_completion_rate",
        "total_duels_lost",

        "aerial_duels_completed",
        "aerial_duels_completion_rate",
    ],

    # -------------------------
    # GOALKEEPING
    # -------------------------
    "goalkeeping": [
        "saves",
        "saves_inside_box",
        "diving_saves",
        "penalties_saved",

        "goals_conceded_goalkeeper",
        "goals_prevented",
    ],

    # -------------------------
    # AGGREGATE PERFORMANCE
    # -------------------------
    "overall": [
        "fotmob_rating",
        "fotmob_fantasy_bonus",
        "fotmob_fantasy_points",
    ],
}


COLUMN_GROUPS = {

    "metadata": [
        "match_id",
        "date",
        "season",
        "team",
        "opponent",
        "player_id",
        "player_name",
        "starter",
        "position",
        "minutes_played",
        "appeared",
        "fotmob_player_stats_status",
        "shirt_number",
        "fotmob_opta_id",
        "fotmob_position_id",
        "fotmob_age_reported",
        "fotmob_country",
        "fotmob_market_value",
        "postion",
    ],

    "role": [
        "ball_carries",
        "carry_distance_m",
        "corners_taken",
        "touches",
        "touches_opposition_box",

        "passes_attempted",
        "long_balls_attempted",
        "crosses_attempted",
        "through_balls_attempted",
        "own_half_passes_attempted",
        "opposition_half_passes_attempted",

        "passes_into_final_third",
        "line_breaking_passes",
        "progressive_carries",
        "progressive_carry_distance_m",

        "dribbles_attempted",

        "shots",
        "shots_inside_box",
        "shots_outside_box",

        "ground_duels_attempted",
        "total_duels_attempted",
        "aerial_duels_attempted",

        "clearances",
        "headed_clearances",
        "interceptions",
        "tackles",
        "fotmob_defensive_actions",

        "distance_covered_m",
        "fotmob_jogging_distance_m",
        "fotmob_running_distance_m",
        "fotmob_walking_distance_m",
        "high_speed_running_distance_m",
        "running_distance_m",
        "sprinting_distance_m",
        "sprints",

        "keeper_sweeper_actions",
        "keeper_throws",
        "punches",
        "high_claims",

        "recoveries",
    ],

    "performance": [
        "assists",
        "big_chances_created",
        "big_chances_missed",
        "goals",
        "key_passes",
        "xa",
        "xg",
        "non_penalty_xg",
        "xgot",
        "fotmob_xg_plus_xa",

        "fotmob_shot_accuracy",
        "shots_blocked",
        "shots_off_target",
        "shots_on_target",
        "woodwork",
        "penalties_missed",

        "passes_completed",
        "long_balls_completed",
        "crosses_completed",
        "through_balls_completed",
        "own_half_passes_completed",
        "opposition_half_passes_completed",

        "dribbles_completed",

        "ground_duels_completed",
        "total_duels_completed",
        "aerial_duels_completed",
        "total_duels_lost",
        "dribbled_past",

        "defensive_shot_blocks",
        "goal_line_clearances",
        "last_man_tackles",
        "tackles_won_sofascore",

        "dispossessed",
        "possession_lost",
        "unsuccessful_touches",

        "errors_leading_to_goal",
        "errors_leading_to_shot",
        "fouls_committed",
        "fouls_suffered",
        "offsides",
        "own_goals",
        "penalties_conceded",
        "penalties_won",

        "diving_saves",
        "goals_conceded_goalkeeper",
        "goals_prevented",
        "penalties_saved",
        "saves",
        "saves_inside_box",
        "xgot_faced",

        "fotmob_rating",
        "fotmob_fantasy_bonus",
        "fotmob_fantasy_points",

        "passes_completion_rate",
        "long_balls_completion_rate",
        "crosses_completion_rate",
        "through_balls_completion_rate",
        "dribbles_completion_rate",
        "ground_duels_completion_rate",
        "total_duels_completion_rate",
        "aerial_duels_completion_rate",
        "own_half_passes_completion_rate",
        "opposition_half_passes_completion_rate",

        "top_speed_kmh",
    ],
}
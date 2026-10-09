"""Aggregate named event flags. Coordinates/maps remain uninterpreted raw data."""
from datetime import datetime, timezone
from .providerLoader import ProviderLoader

COUNTERS={
 'goals':('goalNormal','penaltyScored'),'assists':('assist',),'touches':('touches',),
 'key_passes':('passKey',),
 'shots':('shotsTotal',),'shots_on_target':('shotOnTarget',),'shots_off_target':('shotOffTarget',),
 'shots_blocked':('shotBlocked',),'shots_inside_box':('shotSixYardBox','shotPenaltyArea'),
 'shots_outside_box':('shotOboxTotal',),'woodwork':('shotOnPost',),
 'big_chances_created':('bigChanceCreated',),'big_chances_missed':('bigChanceMissed',),
 'dispossessed':('dispossessed',),'unsuccessful_touches':('turnover',),'fouls_suffered':('foulGiven',),
 'fouls_committed':('foulCommitted',),'offsides':('offsideGiven',),'tackles':('tackleWon','tackleLost'),
 'interceptions':('interceptionAll',),'clearances':('clearanceTotal',),'headed_clearances':('clearanceHead',),
 'defensive_shot_blocks':('outfielderBlock',),'recoveries':('ballRecovery',),'dribbled_past':('challengeLost',),
 'last_man_tackles':('tackleLastMan',),'goal_line_clearances':('clearanceOffTheLine',),
 'errors_leading_to_shot':('errorLeadsToShot',),'errors_leading_to_goal':('errorLeadsToGoal',),
 'penalties_won':('penaltyWon',),'penalties_conceded':('penaltyConceded',),
 'passes_completed':('passAccurate',),'passes_attempted':('passAccurate','passInaccurate'),
 'crosses_completed':('passCrossAccurate',),'crosses_attempted':('passCrossAccurate','passCrossInaccurate'),
 'long_balls_completed':('passLongBallAccurate',),'long_balls_attempted':('passLongBallAccurate','passLongBallInaccurate'),
 'through_balls_completed':('passThroughBallAccurate',),
 'through_balls_attempted':('passThroughBallAccurate','passThroughBallInaccurate','passThroughBallInacurate'),
 'dribbles_completed':('dribbleWon',),'dribbles_attempted':('dribbleWon','dribbleLost'),
 'aerial_duels_completed':('duelAerialWon',),'aerial_duels_attempted':('duelAerialWon','duelAerialLost'),
}


class WhoScoredLoader(ProviderLoader):
    provider='whoscored'

    def decode(self,payload,event=None):
        d=payload['matchCentreData'];types=payload['matchCentreEventTypeJson']
        if d.get('statusCode')!=6 or not d.get('events'):raise ValueError('WhoScored requires finished, populated event data')
        if event is None:raise ValueError('Pass the UTC season fixture to the WhoScored loader')
        if datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat()!=d['startDate'][:10]:
            raise ValueError('WhoScored fixture date does not match its event response')
        ev={'id':payload['match_id'],'startTimestamp':event['startTimestamp'],'status':{'type':'finished'}}
        result={'event':ev}; by_player={}
        for entry in d['events']:
            if entry.get('playerId') is not None:
                by_player.setdefault((entry['teamId'],entry['playerId']),[]).append(entry)
        for side in ('home','away'):
            team=d[side];ev[side+'Team']={'id':team['teamId'],'name':team['name']}
            ev[side+'Score']={'current':team['scores']['fulltime']}
            players=[]
            for player in team['players']:
                events=by_player.get((team['teamId'],player['playerId']),[])
                def count(names):
                    # Alias flags on the SAME event count once, never twice.
                    required=set(names)-{'passThroughBallInaccurate','passThroughBallInacurate'}
                    if not required.issubset(types):return None
                    if 'passThroughBallInaccurate' in names and not {'passThroughBallInaccurate','passThroughBallInacurate'}.intersection(types):return None
                    codes={types[n] for n in names if n in types}
                    if not codes:return None
                    return sum(bool(codes.intersection(e.get('satisfiedEventsTypes',[]))) for e in events)
                metrics={metric:count(names) for metric,names in COUNTERS.items()}
                starter=player.get('isFirstEleven',False)
                appeared=starter or any(types.get('subOn') in e.get('satisfiedEventsTypes',[]) for e in events) or 'subbedInExpandedMinute' in player
                # Minutes deliberately come from the baseline/FotMob. Expanded minutes
                # include stoppage time and are not interchangeable with their convention.
                players.append({'id':player['playerId'],'name':player['name'],'shirt_number':player.get('shirtNo'),
                    'starter':starter,'appeared':appeared,'position':player.get('position'),
                    'raw':player,'metrics':metrics})
            formations=team.get('formations',[])
            result[side]={'formation':formations[0].get('formationName') if formations else None,'players':players}
        return result

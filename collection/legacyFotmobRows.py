"""Read historical FotMob-only artifacts; no live collector."""
from providers.fotMobLoader import FotMobLoader
from .playerStatsSchema import fotmob_column,PLAYER_MATCH_COLUMNS,FOTMOB_COLUMNS
from .selection import season_label
from datetime import datetime,timezone
def player_rows(payload, competition, season, event):
    decoded = FotMobLoader().decode(payload)
    actual = decoded['event']
    if actual['id'] != event['id']:
        raise ValueError('FotMob fixture match ID mismatch')
    if actual['startTimestamp']//86400 != event['startTimestamp']//86400:
        raise ValueError('FotMob fixture date mismatch')
    for side in ('home','away'):
        if (actual[side+'Team']['id'] != event[side+'Team']['id'] or
                actual[side+'Score'] != event[side+'Score']):
            raise ValueError('FotMob fixture team or score mismatch')
    rows = []
    for side, other in (('home','away'),('away','home')):
        for player in decoded[side]['players']:
            row = dict.fromkeys(PLAYER_MATCH_COLUMNS)
            row.update(competition=competition,season=season_label(season),
                match_id=f"fmb_{actual['id']}",
                date=datetime.fromtimestamp(actual['startTimestamp'],timezone.utc).date().isoformat(),
                team_id=f"fmb_{actual[side+'Team']['id']}",team=actual[side+'Team']['name'],
                opponent_id=f"fmb_{actual[other+'Team']['id']}",opponent=actual[other+'Team']['name'],
                player_id=f"fmb_{player['id']}",player_name=player['name'],
                starter=player['starter'],appeared=player['appeared'],
                position=player['position'],shirt_number=player['shirt_number'],
                fotmob_player_stats_status=player['data_status'])
            if player['appeared']:
                row.update({name:value for name,value in player['metrics'].items()
                            if name in FOTMOB_COLUMNS})
                for stem in ('passes','long_balls','crosses','dribbles','ground_duels',
                             'total_duels','aerial_duels'):
                    attempted=row.get(stem+'_attempted');completed=row.get(stem+'_completed')
                    if attempted is not None and completed is not None and attempted > 0:
                        row[stem+'_completion_rate']=completed/attempted
            profile=player.get('lineup_profile',{})
            row.update(fotmob_opta_id=player.get('opta_id'),fotmob_position_id=profile.get('positionId'),
                fotmob_age_reported=profile.get('age'),fotmob_country=profile.get('countryName'),
                fotmob_market_value=profile.get('marketValue'))
            rows.append({fotmob_column(k):v for k,v in row.items()})
    return rows

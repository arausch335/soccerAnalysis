"""FotMob adaptation and player-match enrichment, with source stats retained."""
from .providerLoader import ProviderLoader
from .events import timestamp
from .statistics import fotmob_stats


class FotMobLoader(ProviderLoader):
    provider='fotmob'

    def decode(self,payload,event=None):
        general=payload['general'];content=payload['content'];lineup=content.get('lineup')
        if not general.get('finished') or not lineup:raise ValueError('FotMob requires a finished match with lineups')
        header=payload['header'];teams=header['teams']
        ev={'id':int(general['matchId']),'startTimestamp':timestamp(general['matchTimeUTCDate']),
            'status':{'type':'finished'}}
        result={'event':ev}
        for index,side in enumerate(('home','away')):
            team=lineup[side+'Team'];ev[side+'Team']={'id':int(team['id']),'name':team['name']}
            if int(teams[index]['id'])!=int(team['id']):raise ValueError('FotMob team order changed')
            ev[side+'Score']={'current':int(teams[index]['score'])}
            players=[]
            for starter,entries in ((True,team['starters']),(False,team['subs'])):
                for entry in entries:
                    player=(content.get('playerStats') or {}).get(str(entry['id']))
                    data_status='available' if player is not None else 'missing_player_stats'
                    if player is None:
                        player={'id':entry['id'],'teamId':team['id'],'stats':[]}
                    if player['teamId']!=team['id']:raise ValueError('FotMob player belongs to another team')
                    raw,metrics=fotmob_stats(player)
                    # FotMob's incident timeline is intentionally not consumed.
                    # Participation comes only from the lineup and player stats.
                    appeared=starter or (metrics.get('minutes_played') or 0)>0
                    shots=[s for s in content.get('shotmap',{}).get('shots',[]) if s['playerId']==entry['id'] and s['teamId']==team['id'] and not s.get('isOwnGoal')]
                    # Only use the classified map when its count agrees with a supplied total.
                    if 'shotmap' in content and metrics.get('shots')==len(shots) and all(isinstance(s.get('isFromInsideBox'),bool) for s in shots):
                        metrics['shots_inside_box']=sum(s['isFromInsideBox'] for s in shots)
                        metrics['shots_outside_box']=sum(not s['isFromInsideBox'] for s in shots)
                    coarse={0:'G',1:'D',2:'M',3:'F'}.get(entry.get('usualPlayingPositionId'))
                    players.append({'id':int(entry['id']),'name':entry['name'],'shirt_number':int(entry['shirtNumber']) if entry.get('shirtNumber') else None,
                        'starter':starter,'appeared':appeared,'position':coarse,'raw':raw,'metrics':metrics,
                        'opta_id':player.get('optaId'),'data_status':data_status,
                        'lineup_profile':dict(entry)})
            result[side]={'formation':team.get('formation'),'players':players}
        return result

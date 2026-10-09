"""Inventory cached JSON files containing provider spatial maps."""
from pathlib import Path
from .jsonFiles import read_json
from .selection import season_label


SPATIAL_MAP_COLUMNS = (
    'match_id','date','season','competition','team','opponent','player_id','player_name',
    'provider','provider_player_id','map_type','json_file','json_path','status',
)


def _saved(directory,relative):
    if not relative:return None
    path=Path(directory)/relative
    if not path.exists():return None
    return read_json(path)


def _row(match,provider,map_type,relative,json_path,status,appearance=None,provider_player_id=None):
    return {'match_id':match.id,'date':match.kickoff.date().isoformat(),
        'season':season_label(match.season.name),'competition':match.season.competition.name,
        'team':appearance.team.name if appearance else None,
        'opponent':appearance.opponent.name if appearance else None,
        'player_id':appearance.player.id if appearance else None,
        'player_name':appearance.player.name if appearance else None,
        'provider':provider,'provider_player_id':provider_player_id,'map_type':map_type,
        'json_file':relative,'json_path':json_path,'status':status}


def _list_status(value):
    return 'available' if isinstance(value,list) and value else 'available_empty'


def match_map_rows(match,directory):
    """One row per available match-level spatial payload."""
    rows=[];files=match.metadata.get('sofascore_spatial_files',{})
    for key,map_type,json_path in (
            ('average_positions','average_positions','payload.home|payload.away'),
            ('shotmap','shot_map','payload.shotmap')):
        relative=files.get(key);saved=_saved(directory,relative)
        if not saved:continue
        payload=saved.get('payload',{})
        value=(payload.get('shotmap') if key=='shotmap' else
               [*payload.get('home',[]),*payload.get('away',[])])
        if isinstance(value,list):
            rows.append(_row(match,'sofascore',map_type,relative,json_path,_list_status(value)))
    for provider,json_path in (('fotmob','payload.content.shotmap.shots'),
                               ('whoscored','payload.matchCentreData.events')):
        relative=match.metadata.get('raw_response_files',{}).get(provider);saved=_saved(directory,relative)
        if not saved:continue
        payload=saved.get('payload',{})
        value=((payload.get('content',{}).get('shotmap') or {}).get('shots') if provider=='fotmob'
               else payload.get('matchCentreData',{}).get('events'))
        if isinstance(value,list):
            map_type='shot_map' if provider=='fotmob' else 'event_map'
            rows.append(_row(match,provider,map_type,relative,json_path,_list_status(value)))
    return rows


def player_map_rows(match,appearance,directory):
    """Point to each available player map without copying or deriving coordinates."""
    rows=[];files=match.metadata.get('sofascore_spatial_files',{}).get(str(appearance.player.id),{})
    relative=files.get('heatmap');saved=_saved(directory,relative)
    if saved and isinstance(saved.get('payload',{}).get('heatmap'),list):
        value=saved['payload']['heatmap']
        rows.append(_row(match,'sofascore','heatmap',relative,'payload.heatmap',
                         _list_status(value),appearance,appearance.player.id))
    relative=files.get('rating_breakdown');saved=_saved(directory,relative)
    if saved and isinstance(saved.get('payload'),dict):
        for key,value in saved['payload'].items():
            if not isinstance(value,list):continue
            map_type={'passes':'pass_map','dribbles':'dribble_map'}.get(key,key+'_map')
            rows.append(_row(match,'sofascore',map_type,relative,'payload.'+key,
                             _list_status(value),appearance,appearance.player.id))
    who=appearance.stats.raw.get('_providers',{}).get('whoscored',{})
    relative=match.metadata.get('raw_response_files',{}).get('whoscored');saved=_saved(directory,relative)
    events=(saved or {}).get('payload',{}).get('matchCentreData',{}).get('events')
    if who and isinstance(events,list):
        types={event.get('type',{}).get('displayName') for event in events
               if event.get('playerId')==who.get('id')}
        for event_type,map_type in (('Pass','pass_map'),('TakeOn','dribble_map'),
                                    ('Goal','shot_map'),('MissedShots','shot_map'),
                                    ('SavedShot','shot_map'),('ShotOnPost','shot_map')):
            if event_type in types and not any(r['map_type']==map_type and r['provider']=='whoscored' for r in rows):
                rows.append(_row(match,'whoscored',map_type,relative,
                    'payload.matchCentreData.events', 'available',appearance,who.get('id')))
    return rows

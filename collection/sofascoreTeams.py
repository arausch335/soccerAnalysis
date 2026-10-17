"""Export saved Sofascore team statistics in the existing provider-labelled format."""
from .jsonFiles import read_json
from .selection import season_label
from pathlib import Path

TEAM_STAT_COLUMNS=('match_id','date','season','competition','provider','provider_match_id',
    'fetched_at','side','provider_team_id','team','matchweek','period','group','field',
    'label','value','format','type')


def team_stat_rows(match,directory):
    relative=match.metadata.get('raw_response_files',{}).get('sofascore_statistics')
    if not relative:return []
    saved=read_json(Path(directory)/relative)
    if saved.get('provider')!='sofascore' or saved.get('key')!=f'statistics/{match.id}':
        raise ValueError('Sofascore team statistics cache identity mismatch')
    rows=[]
    for period in saved['payload'].get('statistics',[]):
        for group in period.get('groups',[]):
            for item in group.get('statisticsItems',[]):
                for side in ('home','away'):
                    team=getattr(match,side+'_team')
                    rows.append({'match_id':match.id,'date':match.kickoff.date().isoformat(),
                        'season':season_label(match.season.name),'competition':match.season.competition.name,
                        'provider':'sofascore','provider_match_id':match.id,'fetched_at':saved.get('fetched_at'),
                        'side':side,'provider_team_id':team.id,'team':team.name,'matchweek':match.round_number,
                        'period':period.get('period'),'group':group.get('groupName'),
                        'field':item.get('key',item.get('name')),'label':item.get('name'),
                        'value':item.get(side+'Value',item.get(side)),
                        'format':item.get('valueType'),'type':item.get('statisticsType')})
    return rows

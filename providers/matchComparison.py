"""Small, auditable provider comparisons for Main; raw responses stay on workers."""
from datetime import datetime,timezone
from .scores import final_score


def fixture_summary(event,provider):
    summary={'provider':provider,'match_id':str(event['id']),
             'date':datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat(),
             'home_team':event['homeTeam']['name'],'away_team':event['awayTeam']['name'],
             'home_score':final_score(event,'home'),'away_score':final_score(event,'away')}
    for key,label in (('normaltime','regulation'),('overtime','extra_time'),('penalties','penalties')):
        scores=[(event.get(side+'Score') or {}).get(key) for side in ('home','away')]
        if all(value is not None for value in scores):summary[label]=scores
    return summary


def comparison(match,provider,event,issue):
    baseline=match.metadata.get('id_namespace','sofascore')
    source=match.metadata.get('event') or {'id':match.id,'startTimestamp':int(match.kickoff.timestamp()),
        'homeTeam':{'name':match.home_team.name},'awayTeam':{'name':match.away_team.name},
        'homeScore':{'current':match.home_score},'awayScore':{'current':match.away_score}}
    return {'issue':issue,'baseline':fixture_summary(source,baseline),'source':fixture_summary(event,provider)}


def provider_error(provider,error):
    message=str(error);prefix=provider+':'
    while message.lower().startswith(prefix.lower()):message=message[len(prefix):].lstrip()
    return provider+': '+message

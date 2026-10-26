"""Small provider-neutral fixture dictionaries consumed by all domain loaders."""
from datetime import datetime
from .scores import whoscored_scores


def timestamp(value):
    date=datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None: raise ValueError('Fixture kickoff must have an explicit timezone')
    return int(date.timestamp())

def fotmob_fixture(row,*,competition_id=None):
    status=row['status']; scores=status.get('scoreStr','').split(' - ')
    fixture={'id':int(row['id']),'startTimestamp':timestamp(status['utcTime']),
        'homeTeam':{'id':int(row['home']['id']),'name':row['home']['name']},
        'awayTeam':{'id':int(row['away']['id']),'name':row['away']['name']},
        'homeScore':{'current':int(scores[0]) if len(scores)==2 else None},
        'awayScore':{'current':int(scores[1]) if len(scores)==2 else None},
        'status':{'type':'finished' if status.get('finished') and not status.get('awarded') else 'notstarted'}}
    if competition_id==9081:
        fixture['tournament']={'name':'Bundesliga, Relegation/Promotion Playoffs','uniqueTournament':{'id':9081}}
    return fixture


def whoscored_fixture(row):
    return {'id':int(row['id']),'startTimestamp':timestamp(row['startTimeUtc']),
        'homeTeam':{'id':row['homeTeamId'],'name':row['homeTeamName']},
        'awayTeam':{'id':row['awayTeamId'],'name':row['awayTeamName']},
        'homeScore':whoscored_scores({'fulltime':row.get('homeScore'),'extratime':row.get('homeExtratimeScore'),'penalty':row.get('homePenaltyScore')}),
        'awayScore':whoscored_scores({'fulltime':row.get('awayScore'),'extratime':row.get('awayExtratimeScore'),'penalty':row.get('awayPenaltyScore')}),
        'status':{'type':'finished' if row.get('status')==6 else 'notstarted'}}

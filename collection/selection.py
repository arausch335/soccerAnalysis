"""Resolve friendly competition and season strings to verified provider IDs."""
from copy import deepcopy
from dataclasses import dataclass
import re
from providers.identity import name_key
from providers.sofascore_ids import SOFASCORE_COMPETITION_IDS


@dataclass(frozen=True)
class CompetitionSelection:
    name:str
    sofascore_id:int
    fotmob_id:int
    whoscored_path:str
    fotmob_supplemental_ids:tuple[int,...]=()
    whoscored_supplemental_paths:tuple[str,...]=()
    parent:str|None=None


COMPETITIONS=[
    CompetitionSelection('Premier League',SOFASCORE_COMPETITION_IDS['Premier League'],47,'regions/252/tournaments/2/england-premier-league'),
    CompetitionSelection('La Liga',SOFASCORE_COMPETITION_IDS['La Liga'],87,'regions/206/tournaments/4/spain-laliga'),
    CompetitionSelection('Bundesliga',SOFASCORE_COMPETITION_IDS['Bundesliga'],54,'regions/81/tournaments/3/germany-bundesliga',
        fotmob_supplemental_ids=(9081,),
        whoscored_supplemental_paths=('regions/81/tournaments/388/germany-bundesliga-qualification',)),
    CompetitionSelection('Serie A',SOFASCORE_COMPETITION_IDS['Serie A'],55,'regions/108/tournaments/5/italy-serie-a'),
    CompetitionSelection('Ligue 1',SOFASCORE_COMPETITION_IDS['Ligue 1'],53,'regions/74/tournaments/22/france-ligue-1'),
    CompetitionSelection('Champions League',SOFASCORE_COMPETITION_IDS['Champions League'],42,
        'regions/250/tournaments/12/europe-champions-league'),
    CompetitionSelection('Europa League',SOFASCORE_COMPETITION_IDS['Europa League'],73,
        'regions/250/tournaments/30/europe-europa-league'),
    CompetitionSelection('Champions League Qualifiers',SOFASCORE_COMPETITION_IDS['Champions League'],10611,
        'regions/250/tournaments/12/europe-champions-league',parent='Champions League'),
    CompetitionSelection('Europa League Qualifiers',SOFASCORE_COMPETITION_IDS['Europa League'],10613,
        'regions/250/tournaments/30/europe-europa-league',parent='Europa League'),
]


# Results reviewed against the governing competition after a provider left the
# fixture in an abandoned state. They belong in schedules and result tables,
# but no lineup or event data is invented for them.
REVIEWED_SCHEDULE_RESULTS={
    ('Ligue 1','2025/2026'):{
        'id':14061912,'startTimestamp':1779044400,
        'homeTeam':{'id':1647,'name':'Nantes'},
        'awayTeam':{'id':1681,'name':'Toulouse'},
        'roundInfo':{'round':34},
        'homeScore':{'current':0,'display':0,'normaltime':0},
        'awayScore':{'current':0,'display':0,'normaltime':0},
        'winnerCode':3,
    },
}


def with_reviewed_schedule_results(competition,season,season_id,events):
    """Return the provider schedule with reviewed official results applied."""
    result=[deepcopy(event) for event in events]
    correction=REVIEWED_SCHEDULE_RESULTS.get((competition.name,season))
    if correction is None:return result
    existing=next((event for event in result if event.get('id')==correction['id']),None)
    if existing is None:
        existing={
            'season':{'id':season_id,'name':season},
            'tournament':{'name':competition.name,
                'uniqueTournament':{'id':competition.sofascore_id,'name':competition.name}},
        }
        result.append(existing)
    existing.update(deepcopy(correction))
    existing.setdefault('season',{}).update(id=season_id)
    existing.setdefault('tournament',{}).setdefault('uniqueTournament',{}).update(
        id=competition.sofascore_id)
    existing['status']={'type':'finished','description':'Official reviewed result','awarded':True}
    existing['isAwarded']=True
    existing['reviewedResult']=True
    return result


def is_qualification_event(event,competition=None):
    """True only for pre-main-competition qualification fixtures."""
    if competition=='Bundesliga':return False  # Promotion/relegation playoffs belong to the league season.
    return bool((event.get('tournament') or {}).get('qualificationOrPreliminary'))


def competition_phase(competition,event):
    tournament=event.get('tournament') or {};round_info=event.get('roundInfo') or {}
    labels=' '.join(str(value or '') for value in (tournament.get('name'),tournament.get('slug'),
                                                   round_info.get('name'),round_info.get('slug')))
    if competition=='Bundesliga' and re.search(r'relegation|relegations?|play[ -]?offs?',labels,re.I):
        return 'playoffs'
    return 'qualifying' if is_qualification_event(event,competition) else 'main'


def in_competition(event,competition):
    if competition.parent:return is_qualification_event(event)
    if competition.name in ('Champions League','Europa League'):return not is_qualification_event(event)
    return True


def event_competition(competition,event):
    if competition in ('Champions League','Europa League') and is_qualification_event(event):return competition+' Qualifiers'
    return competition


def competition_by_name(name):
    aliases={'epl':'premierleague','englishpremierleague':'premierleague','laliga':'laliga',
             'ucl':'championsleague','uefachampionsleague':'championsleague',
             'uel':'europaleague','uefaeuropaleague':'europaleague',
             'league1':'ligue1','franceligue1':'ligue1'}
    key=aliases.get(name_key(name),name_key(name))
    found=[c for c in COMPETITIONS if name_key(c.name)==key]
    if len(found)!=1:raise ValueError('Supported competitions: '+', '.join(c.name for c in COMPETITIONS))
    return found[0]


def season_key(value):
    value=str(value).strip().replace('–','-').replace('—','-')
    found=re.fullmatch(r'(\d{4})(?:\s*[-/]\s*(\d{2}|\d{4}))?',value)
    if not found:raise ValueError('Use a season such as 2024-2025, 2024/25, or 2024')
    year=int(found.group(1));end=found.group(2)
    next_year=(int(end) if len(end or '')==4 else year//100*100+int(end)) if end else year+1
    if next_year==year-99:next_year+=100
    if next_year!=year+1:raise ValueError('These leagues require consecutive season years')
    return f'{year}/{next_year}'


def season_label(value):
    """CSV-facing season label, separate from provider-specific season keys."""
    value=str(value).strip().replace('–','-').replace('—','-')
    full=re.search(r'(\d{4})\s*[-/]\s*(\d{2}|\d{4})\s*$',value)
    if full:return season_key(full.group(0)).replace('/','-')
    short=re.search(r'(?<!\d)(\d{2})\s*[-/]\s*(\d{2})\s*$',value)
    if short:
        start=2000+int(short.group(1));end=2000+int(short.group(2))
        if end!=start+1:raise ValueError(f'Invalid season label {value!r}')
        return f'{start}-{end}'
    return season_key(value).replace('/','-')


def resolve_sofascore_season(client,competition,season):
    canonical=season_key(season);start,end=canonical.split('/')
    keys={canonical,f'{start[-2:]}/{end[-2:]}'}
    found=[s for s in client.seasons(competition.sofascore_id)['seasons'] if s.get('year') in keys]
    if len(found)!=1:raise ValueError(f'Cannot uniquely resolve {competition.name} {canonical} on Sofascore')
    return found[0]


def months_between(events):
    from datetime import datetime,timezone
    if not events:return []
    first=datetime.fromtimestamp(min(e['startTimestamp'] for e in events),timezone.utc)
    last=datetime.fromtimestamp(max(e['startTimestamp'] for e in events),timezone.utc)
    y,m=first.year,first.month;result=[]
    while (y,m)<=(last.year,last.month):
        result.append(f'{y:04d}{m:02d}');m+=1
        if m==13:y+=1;m=1
    return result


def configured_whoscored_stages(competition,season):
    """Only use explicitly verified IDs; never derive a stage ID from a year."""
    import config
    return config.WHOSCORED_STAGE_IDS.get(f'{competition.name} {season_label(season)}')

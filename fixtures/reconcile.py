"""Conservative fixture-attempt reconciliation; provider records stay untouched."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json

from collection.selection import competition_phase
from providers.scores import final_score

DOMESTIC = {'Premier League','Championship','La Liga','La Liga 2','Bundesliga',
            '2. Bundesliga','Serie A','Serie B','Ligue 1','Ligue 2'}
# Official decision: the abandoned 16 Dec game was replayed in full on 13 Mar.
# https://www.premierleague.com/en/news/3830154
REVIEWED_ABANDONED = {('fotmob', 'Premier League', 2023, 4193691): 4421797}
# The opening fixture changed orientation while Bloomfield Road was rebuilt.
# https://www.premierleague.com/en/news/698090
REVIEWED_VENUE_SWAPS = {('sofascore','Premier League',2010,1379596):1380091}
ATTEMPT_COLUMNS = ('competition','season','provider','match_id','canonical_match_id',
                   'reason','original_status','status','home_team_id','away_team_id',
                   'kickoff_timestamp','home_score','away_score','round','round_name','phase')


def reconcile(events, provider, competition, start, *, bridges=None):
    """Only collapse a uniquely proven league replacement, never two cup legs."""
    events = deepcopy(events)
    original = {e['id']: deepcopy(e) for e in events}
    by_pair = defaultdict(list)
    for event in events:
        correction = event.get('reviewedReplacement') or REVIEWED_ABANDONED.get((provider,competition,start,event['id']))
        if correction:
            event['status'] = {**event.get('status',{}), 'type':'abandoned'}
            event['reviewedReplacement'] = correction
        phase = competition_phase(competition,event)
        by_pair[phase, str(event['homeTeam']['id']), str(event['awayTeam']['id'])].append(event)
    removed, audit = set(), []
    all_ids = {e['id']:e for e in events}
    for (phase, _, _), attempts in by_pair.items():
        for event in attempts:
            old_status = event.get('status',{}).get('type','unknown')
            if old_status not in ('postponed','canceled','cancelled','abandoned','notstarted','inprogress'):continue
            venue_swap = REVIEWED_VENUE_SWAPS.get((provider,competition,start,event['id']))
            target = all_ids.get(venue_swap or event.get('reviewedReplacement'))
            if target is not None:
                if venue_swap:
                    if not (str(target['homeTeam']['id'])==str(event['awayTeam']['id']) and
                            str(target['awayTeam']['id'])==str(event['homeTeam']['id']) and
                            target['startTimestamp']//86400==event['startTimestamp']//86400 and
                            target.get('status',{}).get('type')=='finished'):target=None
                elif target not in attempts:target=None
            if target is None and not venue_swap and not event.get('reviewedReplacement'):
                candidates = []
                for other in attempts:
                    if other['id']==event['id']:continue
                    if other.get('status',{}).get('type')!='finished' and other['startTimestamp']<=event['startTimestamp']:continue
                    if other.get('status',{}).get('type') not in ('finished','inprogress','notstarted'):continue
                    a,b = (e.get('roundInfo') or {} for e in (event,other))
                    linked=bridges is not None and bridges.same('match',provider,event['id'],provider,other['id']) is True
                    if a.get('round') is not None and b.get('round') is not None and a['round']!=b['round']:continue
                    # A named stage/leg must agree when present on both attempts.
                    if any(a.get(k) is not None and b.get(k) is not None and a[k]!=b[k] for k in ('name','leg')):continue
                    same_round=any(a.get(k) is not None and a.get(k)==b.get(k) for k in ('round','name','leg'))
                    if not linked and not same_round and not (competition in DOMESTIC and phase=='main'):continue
                    candidates.append(other)
                if bridges is not None:
                    published=[other for other in candidates if bridges.same('match',provider,event['id'],provider,other['id']) is True]
                    if published:candidates=published
                # A played fixture supersedes both old abandoned/postponed
                # attempts and redundant future schedule entries for that round.
                played=[e for e in candidates if e.get('status',{}).get('type')=='finished']
                if played:candidates=played
                elif old_status in ('notstarted','inprogress'):candidates=[]
                if len(candidates)==1:target=candidates[0]
            if target is not None:
                removed.add(event['id'])
            if target is not None or event.get('reviewedReplacement'):
                raw=original[event['id']]
                audit.append(dict(competition=competition,season=f'{start}-{start+1}',provider=provider,
                    match_id=event['id'],canonical_match_id=target['id'] if target else '',
                    reason='reviewed_venue_swap' if venue_swap else 'reviewed_abandoned_replay' if event.get('reviewedReplacement') else 'superseded_fixture',
                    original_status=raw.get('status',{}).get('type','unknown'),status=old_status,
                    home_team_id=event['homeTeam']['id'],away_team_id=event['awayTeam']['id'],
                    kickoff_timestamp=event['startTimestamp'],home_score=final_score(raw,'home'),away_score=final_score(raw,'away'),
                    round=(raw.get('roundInfo') or {}).get('round'),
                    round_name=(raw.get('roundInfo') or {}).get('name'),phase=phase))
    return [e for e in events if e['id'] not in removed], audit


def prune_exported_fixtures(rows,bridges):
    """Apply the same played/replay policy to already received CSV fixtures."""
    groups=defaultdict(list);original={};audit=[]
    for row in rows:
        mid=str(row.get('sofascore_match_id') or '')
        if not mid or not row.get('kickoff_utc') or not row.get('sofascore_home_team_id') or not row.get('sofascore_away_team_id'):
            continue
        event=dict(id=int(mid),startTimestamp=int(datetime.fromisoformat(row['kickoff_utc']).timestamp()),
                   status={'type':row.get('status','')},
                   homeTeam={'id':row['sofascore_home_team_id']},awayTeam={'id':row['sofascore_away_team_id']},
                   roundInfo={k:v for k,v in {'round':row.get('matchweek'),'name':row.get('round_name')}.items() if v},
                   tournament={'qualificationOrPreliminary':row.get('competition_phase')=='qualifying',
                               'name':'Promotion/Relegation Playoffs' if row.get('competition_phase')=='playoffs' else row.get('competition','')})
        competition=row['competition'];start=int(row['season'][:4])
        # A reviewed replay may be recorded under any provider's native ID.
        for provider in ('sofascore','fotmob','whoscored'):
            native=row.get(provider+'_match_id')
            if not native:continue
            replacement=REVIEWED_ABANDONED.get((provider,competition,start,int(native)))
            if replacement:
                event['_replacement_provider']=provider;event['_replacement_native']=replacement
        groups[competition,start].append(event);original[event['id']]=row
    removed=set()
    for (competition,start),events in groups.items():
        # Translate a reviewed provider replay link into the baseline namespace.
        for event in events:
            provider=event.get('_replacement_provider')
            if provider:
                targets=[e for e in events if original[e['id']].get(provider+'_match_id')==str(event['_replacement_native'])]
                if len(targets)==1:event['reviewedReplacement']=targets[0]['id']
        kept,details=reconcile(events,'sofascore',competition,start,bridges=bridges)
        removed.update({e['id'] for e in events}-{e['id'] for e in kept});audit.extend(details)
    return [row for row in rows if int(row.get('sofascore_match_id') or 0) not in removed],audit


def summarize(events, canonical, competition):
    phases = {}
    for phase in sorted({competition_phase(competition,e) for e in (*events,*canonical)}):
        raw = [e for e in events if competition_phase(competition,e)==phase]
        kept = [e for e in canonical if competition_phase(competition,e)==phase]
        phases[phase] = dict(raw=len(raw),canonical=len(kept),
            statuses=dict(Counter(e.get('status',{}).get('type','unknown') for e in kept)))
    return dict(raw_fixtures=len(events),canonical_fixtures=len(canonical),
                finished_fixtures=sum(e.get('status',{}).get('type')=='finished' for e in canonical),
                superseded_fixtures=len(events)-len(canonical),phases=json.dumps(phases,sort_keys=True))

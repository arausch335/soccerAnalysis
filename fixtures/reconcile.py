"""Conservative fixture-attempt reconciliation; provider records stay untouched."""
from collections import Counter, defaultdict
from copy import deepcopy
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


def reconcile(events, provider, competition, start):
    """Only collapse a uniquely proven league replacement, never two cup legs."""
    events = deepcopy(events)
    original = {e['id']: deepcopy(e) for e in events}
    by_pair = defaultdict(list)
    for event in events:
        correction = REVIEWED_ABANDONED.get((provider,competition,start,event['id']))
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
            if old_status not in ('postponed','canceled','cancelled','abandoned'):continue
            venue_swap = REVIEWED_VENUE_SWAPS.get((provider,competition,start,event['id']))
            target = all_ids.get(venue_swap or event.get('reviewedReplacement'))
            if target is not None:
                if venue_swap:
                    if not (str(target['homeTeam']['id'])==str(event['awayTeam']['id']) and
                            str(target['awayTeam']['id'])==str(event['homeTeam']['id']) and
                            target['startTimestamp']//86400==event['startTimestamp']//86400 and
                            target.get('status',{}).get('type')=='finished'):target=None
                elif target not in attempts:target=None
            if target is None and not venue_swap and not event.get('reviewedReplacement') and competition in DOMESTIC and phase == 'main':
                candidates = []
                for other in attempts:
                    if other['id']==event['id']:continue
                    if other.get('status',{}).get('type')!='finished' and other['startTimestamp']<=event['startTimestamp']:continue
                    if other.get('status',{}).get('type') not in ('finished','inprogress','notstarted'):continue
                    a,b = (e.get('roundInfo') or {} for e in (event,other))
                    if a.get('round') is not None and b.get('round') is not None and a['round']!=b['round']:continue
                    # A named stage/leg must agree when present on both attempts.
                    if any(a.get(k) is not None and b.get(k) is not None and a[k]!=b[k] for k in ('name','leg')):continue
                    candidates.append(other)
                if len(candidates)==1:target=candidates[0]
            if target is not None:
                removed.add(event['id'])
            if target is not None or event.get('reviewedReplacement'):
                raw=original[event['id']]
                audit.append(dict(competition=competition,season=f'{start}-{start+1}',provider=provider,
                    match_id=event['id'],canonical_match_id=target['id'] if target else '',
                    reason='reviewed_venue_swap' if venue_swap else 'reviewed_abandoned_replay' if event.get('reviewedReplacement') else 'superseded_league_fixture',
                    original_status=raw.get('status',{}).get('type','unknown'),status=old_status,
                    home_team_id=event['homeTeam']['id'],away_team_id=event['awayTeam']['id'],
                    kickoff_timestamp=event['startTimestamp'],home_score=final_score(raw,'home'),away_score=final_score(raw,'away'),
                    round=(raw.get('roundInfo') or {}).get('round'),
                    round_name=(raw.get('roundInfo') or {}).get('name'),phase=phase))
    return [e for e in events if e['id'] not in removed], audit


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

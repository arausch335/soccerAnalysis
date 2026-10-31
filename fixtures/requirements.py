"""As-of fixture requirements and reviewed domestic playoff applicability."""
from collections import defaultdict
from datetime import datetime,timezone
import json
import re

# Reviewed competition rules, not provider/team-name overrides.
# 2019/20: COVID termination (LFP 30 April 2020).
# 2022/23: https://www.lfp.fr/article/calendrier-general-des-competitions-2022-2023
# 2023/24 L2/National: https://www.fff.fr/article/10751-l-abecedaire-de-l-edition-2023-2024.html
NOT_HELD={('Ligue 1',2019):'season_terminated_covid',('Ligue 2',2019):'season_terminated_covid',
          ('Ligue 1',2022):'division_format_transition',('Ligue 2',2022):'division_format_transition',
          ('Ligue 2',2023):'division_format_transition'}
DOMESTIC_TOURNAMENTS={'Bundesliga':388,'2. Bundesliga':673,'Ligue 1':660,'Ligue 2':661}
COVERAGE_COLUMNS=('required_status','required_fixtures','matched_required_fixtures','unmatched_required_fixtures',
                  'optional_future_fixtures','excluded_unplayed_fixtures')


def instant(value=None):
    stamp=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value or datetime.now(timezone.utc)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def required(row,at=None):
    """Finished fixtures are required; future, cancelled and unplayed rows aren't."""
    if row.get('status')!='finished':return False
    stamp=row.get('kickoff_utc') or row.get('date')
    return not stamp or instant(stamp)<=instant(at)


def _playoff(event):
    title=str((event.get('tournament') or {}).get('name','')).lower()
    return any(word in title for word in ('playoff','play-off','promotion','relegation','qualification','barrage'))


def domestic_phase(competition,start,baseline=(),at=None):
    at=instant(at)
    if any(_playoff(e) and (e.get('status') or {}).get('type')=='finished'
           and e.get('startTimestamp',0)<=at.timestamp() for e in baseline):return 'required'
    if (competition,start) in NOT_HELD:return 'not_applicable'
    # An unfinished season can have unpublished playoff fixtures. Once its
    # season boundary passes, absence must be investigated rather than deferred.
    if competition in DOMESTIC_TOURNAMENTS and datetime(start,7,1,tzinfo=timezone.utc)<=at<datetime(start+1,7,1,tzinfo=timezone.utc):
        return 'not_yet_required'
    return 'required'


def deferred_feed(competition,start,provider,feed,*,at=None):
    return dict(provider=provider,feed=str(feed),reason='not_yet_published',
                recheck_after=datetime(start+1,7,1,tzinfo=timezone.utc).isoformat())


def _details(row):
    value=row.get('scope_details') or '{}'
    return json.loads(value) if isinstance(value,str) else dict(value)


def _baseline(rows):
    return [dict(startTimestamp=instant(r.get('kickoff_utc') or r['date']).timestamp(),status={'type':r.get('status')},
        tournament={'name':'Promotion/Relegation Playoffs' if r.get('competition_phase')=='playoffs' else r.get('competition','')})
        for r in rows if r.get('kickoff_utc') or r.get('date')]


def classify_existing(record,rows,at=None):
    """Upgrade legacy warning wording only when its exact source is established."""
    row=dict(record);detail=_details(row);competition=row['competition'];start=int(row['season'][:4])
    phase=domestic_phase(competition,start,_baseline(rows),at)
    error=row.get('error','');provider=row.get('provider')
    # Never hide month/network errors or an unsuccessful regular-season lookup.
    main_loaded=any('/tournaments/'+str(cid)+'/' in s.get('competition_path','') for s in detail.get('stages',[])
                    for cid in {'Bundesliga':(3,),'2. Bundesliga':(6,),'Ligue 1':(22,),'Ligue 2':(37,)}.get(competition,()))
    who_error=re.fullmatch(r'(?:_PartialSchedule: )?WhoScored season '+re.escape(competition)+rf' {start}/{start+1} unavailable or ambiguous',error or '')
    if row.get('status')=='partial' and provider=='whoscored' and main_loaded and who_error:
        if phase=='not_applicable':
            detail['not_applicable']=[dict(reason=NOT_HELD[competition,start],tournament=DOMESTIC_TOURNAMENTS[competition],previous_warning=error)]
            row.update(status='complete',error='')
        elif phase=='not_yet_required':
            detail['deferred']=[deferred_feed(competition,start,provider,DOMESTIC_TOURNAMENTS[competition],at=at)]
            detail['previous_warning']=error;row.update(status='deferred',error='')
    if (row.get('status')=='partial' and provider=='fotmob' and competition=='Bundesliga'
            and phase=='not_yet_required' and error==f'FotMob feed 9081: No season {start}/{start+1} for FotMob competition 9081'):
        detail['deferred']=[deferred_feed(competition,start,provider,9081,at=at)]
        detail['previous_warning']=error;row.update(status='deferred',error='')
    row['scope_details']=json.dumps(detail,sort_keys=True)
    return row


def coverage(rows,records,at=None):
    at=instant(at);groups=defaultdict(list)
    for row in rows:groups[row.get('competition','').removesuffix(' Qualification'),row.get('season','')].append(row)
    result=[]
    for record in records:
        # Older compiled bundles can lack the schedule-coverage schema. Keep
        # them importable without claiming proof of required schedule scope.
        if (not all(record.get(k) for k in ('competition','season','provider','status'))
                or not re.fullmatch(r'\d{4}-\d{4}',str(record.get('season','')))):
            result.append({**record,'required_status':'partial'})
            continue
        subset=groups[record['competition'],record['season']]
        row=classify_existing(record,subset,at);needed=[r for r in subset if required(r,at)]
        p=row['provider'];matched=sum(p=='sofascore' or r.get(p+'_fixture_check')=='validated' for r in needed)
        detail=_details(row);deferred=detail.get('deferred',[])
        future_deferred=(bool(deferred) and all(instant(d['recheck_after'])>at for d in deferred)
            and domestic_phase(row['competition'],int(row['season'][:4]),_baseline(subset),at)=='not_yet_required')
        row.update(required_status='complete' if row['status']=='complete' or row['status']=='deferred' and future_deferred else 'partial',
            required_fixtures=len(needed),matched_required_fixtures=matched,unmatched_required_fixtures=len(needed)-matched,
            optional_future_fixtures=sum(bool(r.get('kickoff_utc') or r.get('date')) and instant(r.get('kickoff_utc') or r['date'])>at for r in subset),
            excluded_unplayed_fixtures=sum(not required(r,at) and (not (r.get('kickoff_utc') or r.get('date')) or instant(r.get('kickoff_utc') or r['date'])<=at) for r in subset))
        result.append(row)
    return result


def summary(rows,records,at=None):
    at=instant(at);needed=[r for r in rows if required(r,at)]
    return dict(requirements_as_of=at.isoformat(),future_fixture_matching_required=False,required_fixtures=len(needed),
        required_schedule_coverage_complete=bool(records) and all(r.get('required_status')=='complete' for r in records),
        deferred_provider_schedules=sum(r.get('status')=='deferred' for r in records),
        all_required_provider_fixtures_validated=all(r.get(p+'_fixture_check')=='validated' for r in needed for p in ('fotmob','whoscored')))

"""Live collection timing shared across entry points without changing CLI arguments."""
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
import csv
import json
from pathlib import Path
from statistics import mean, median
import time

def waiting_seconds(clients):
    values=[getattr(client,'waiting_seconds',0) for client in clients]
    return sum(value for value in values if type(value) in (int,float))


_ACTIVE=ContextVar('collection_progress',default=None)


def duration(seconds):
    if seconds is None:return 'estimating…'
    seconds=max(0,int(round(seconds)))
    days,seconds=divmod(seconds,86400);hours,seconds=divmod(seconds,3600);minutes,seconds=divmod(seconds,60)
    values=[]
    if days:values.append(f'{days}d')
    if hours or days:values.append(f'{hours}h')
    if minutes or hours or days:values.append(f'{minutes}m')
    if not days and not hours:values.append(f'{seconds}s')
    return ' '.join(values)


def network_count(clients):
    counts=[getattr(client,'network_requests',None) for client in clients]
    if not counts or any(type(value) not in (int,float) for value in counts):return None
    return sum(counts)


class CollectionProgress:
    def __init__(self,*,enabled=True,report=print,clock=None):
        self.enabled=enabled;self.report=report;self.clock=clock or time.perf_counter
        self.started=self.clock();self.jobs={};self.samples=defaultdict(lambda:deque(maxlen=12))
        self.setups=defaultdict(list)

    def emit(self,message):
        if self.enabled:
            if self.report is print:self.report(message,flush=True)
            else:self.report(message)

    def job(self,directory,kind,competition,season):
        key=str(Path(directory).resolve())+('|whoscored_events' if kind=='whoscored_events' else '')
        if key not in self.jobs:self.jobs[key]=JobProgress(self,key,kind,competition,season)
        return self.jobs[key]

    def rate(self,kind,mode=None):
        specific=self.samples.get((kind,mode),()) if mode is not None else ()
        if specific:return median(specific),len(specific),False
        same=[v for (k,_),values in self.samples.items() if k==kind for v in values]
        if same:return median(same),len(same),True
        all_values=[v for values in self.samples.values() for v in values]
        return (median(all_values),len(all_values),True) if all_values else (None,0,True)

    def remaining(self):
        total=0;provisional=False
        for job in self.jobs.values():
            if job.finished:continue
            pending=job.remaining
            if pending is None:
                same=[other.total for other in self.jobs.values()
                      if other.total and other.competition==job.competition and other.kind==job.kind]
                known=same or [other.total for other in self.jobs.values() if other.total and other.kind==job.kind]
                known=known or [other.total for other in self.jobs.values() if other.total]
                if not known:return None,True
                pending=mean(known);provisional=True
            if not pending:continue
            rate,_,rough=self.rate(job.kind,job.mode)
            if rate is None:return None,True
            total+=rate*pending;provisional|=rough
            if job.setup_started is None:
                setup=self.setups.get(job.kind,[])
                if setup:total+=mean(setup)
                else:provisional=True
        return total,provisional

    def summary(self):
        paused=sum(job.paused or (not job.finished and job.remaining!=0) for job in self.jobs.values())
        self.emit(f'[Timing] Collection run finished in {duration(self.clock()-self.started)}'
                  +(f'; {paused} season job(s) paused/incomplete.' if paused else '.'))


class JobProgress:
    def __init__(self,owner,key,kind,competition,season):
        self.owner=owner;self.key=key;self.kind=kind;self.competition=competition;self.season=season
        self.total=None;self.pending=None;self.mode=None;self.finished=False;self.paused=False
        self.setup_started=None;self.setup_seconds=None;self.game_start=None
        self.current=None;self.before=None;self.wait_before=0;self.known_cached=False;self.revisit_complete=False

    @property
    def remaining(self):return len(self.pending) if self.pending is not None else None

    def begin(self):
        self.setup_started=self.owner.clock();self.finished=False;self.paused=False

    def schedule(self,events,*,completed=(),mode=None):
        ids={str(event['id']) for event in events};self.total=len(ids)
        self.pending=ids-set(map(str,completed));self.mode=mode
        if self.setup_started is not None and self.setup_seconds is None:
            self.setup_seconds=self.owner.clock()-self.setup_started
            self.owner.setups[self.kind].append(self.setup_seconds)
        self.owner.emit(f'[Timing] {self.competition} {self.season} | {self.total} selected games; '
                        f'{len(self.pending)} remaining | season setup {duration(self.setup_seconds)}')

    def start_game(self,mid,*,clients=(),cached=False):
        self.current=str(mid);self.game_start=self.owner.clock()
        self.before=network_count(clients);self.known_cached=cached
        self.wait_before=waiting_seconds(clients)

    def finish_game(self,*,clients=(),status='complete',mode=None,cached=False):
        if self.game_start is None:return
        elapsed=max(0,self.owner.clock()-self.game_start)
        excluded=min(elapsed,max(0,waiting_seconds(clients)-self.wait_before))
        active=elapsed-excluded
        after=network_count(clients)
        is_cached=cached or (self.before is not None and after==self.before) or (self.before is None and self.known_cached)
        if self.pending is not None:self.pending.discard(self.current)
        if mode is not None:self.mode=mode
        if status=='complete' and not is_cached:
            self.owner.samples[(self.kind,self.mode)].append(active)
        rate,count,rough=self.owner.rate(self.kind,self.mode)
        season_eta=(0 if self.remaining==0 else
                    rate*self.remaining if rate is not None and self.remaining is not None else None)
        total_eta,provisional=self.owner.remaining()
        calibration=f'calibrating {count}/3' if count<3 else f'{count} recent scraped games'
        paused=sum(j.paused for j in self.owner.jobs.values())
        self.owner.emit(f'[Timing] {self.competition} {self.season} | match {self.current} '
            f'{status if status not in ("complete","partial") else "cached/skipped" if is_cached else status}: {duration(elapsed)}'
            +(f' (active {duration(active)}; waiting {duration(excluded)} excluded from ETA)' if excluded>=1 else '')+' | '
            f'season remaining {"~" if season_eta is not None else ""}{duration(season_eta)} | '
            f'collection remaining {"~" if total_eta is not None else ""}{duration(total_eta)}'
            f'{" (provisional)" if provisional or rough or count<3 else ""} | {calibration}'
            +(f'; excludes {paused} paused season job(s)' if paused else ''))
        self.game_start=None

    def end(self,code=0):
        self.finished=True;self.paused=code!=0


def get_job(directory,kind,competition,season,*,report=print):
    owner=_ACTIVE.get()
    if owner is None:
        import config
        owner=CollectionProgress(enabled=config.SHOW_TIMING,report=report)
    job=owner.job(directory,kind,competition,season)
    # A named resume can resolve to a different saved selection than CLI defaults.
    job.competition=competition;job.season=season
    return job


@contextmanager
def progress_scope(progress):
    token=_ACTIVE.set(progress)
    try:yield progress
    finally:_ACTIVE.reset(token)


def prime_job(job,directory,*,limit=None):
    """Read existing counts locally; do not fetch future schedules for an ETA."""
    directory=Path(directory)
    path=directory/('whoscored_schedule.json' if job.kind=='whoscored_events' else 'schedule.json')
    if not path.exists():return
    try:
        events=json.loads(path.read_text(encoding='utf-8'))['events']
        events=[e for e in events if e.get('status',{}).get('type','finished')=='finished']
        if limit is not None:events=events[:limit]
        completed=set()
        if job.kind in ('combined','sofascore'):
            status=directory/'collection.csv'
            if status.exists():
                with status.open(encoding='utf-8',newline='') as stream:
                    completed={row['match_id'] for row in csv.DictReader(stream) if row['status']=='complete'}
        else:
            from providers.cache import ResponseCache
            provider='whoscored' if job.kind=='whoscored_events' else 'fotmob'
            cache=ResponseCache(directory/'raw')
            completed={str(e['id']) for e in events if cache.path(provider,f"match/{e['id']}").exists()}
        if job.revisit_complete:completed=set()
        job.total=len(events);job.pending={str(e['id']) for e in events}-completed
    except (OSError,ValueError,KeyError,TypeError):pass


@contextmanager
def collection_scope(kind,root,competitions,seasons,*,limit=None):
    """Give legacy batch entry points the same overall ETA as main.py."""
    existing=_ACTIVE.get()
    if existing is not None:
        yield existing
        return
    import config
    import re
    from .selection import competition_by_name, season_label
    owner=CollectionProgress(enabled=config.SHOW_TIMING)
    subtree='' if kind=='whoscored_events' else 'fotmob_player_stats'
    for name in competitions:
        competition=competition_by_name(name).name
        slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
        for season in seasons:
            directory=Path(root)/subtree/slug/season_label(season)
            job=owner.job(directory,kind,competition,season_label(season))
            prime_job(job,directory,limit=limit)
    with progress_scope(owner):
        try:yield owner
        finally:owner.summary()

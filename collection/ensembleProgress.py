"""Portable evidence of completed work; this never represents local raw data."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from storage.pandasStore import atomic_json, PandasStore
from .selection import season_label, competition_by_name
from .workerIdentity import ensure_identity, read_worker_id, read_origins, save_origins

FORMAT = 'soccer-analysis-progress-v1'
ACTIVE = ContextVar('ensemble_progress', default=None)


def read_progress(path):
    saved=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(saved,dict) or saved.get('format')!=FORMAT or not isinstance(saved.get('records'),dict):
        raise ValueError(f'Unsupported ensemble provenance file: {path}')
    if any(not isinstance(row,dict) or any(not isinstance(row.get(field,[]),list)
           or any(not isinstance(item,str) for item in row.get(field,[]))
           for field in ('capabilities','origins','holders')) for row in saved['records'].values()):
        raise ValueError(f'Invalid portable provenance records: {path}')
    return saved


def merge_records(target, incoming):
    for key,row in incoming.items():
        old=target.get(key,{})
        target[key]={**old,**row,
            'capabilities':sorted(set(old.get('capabilities',[]))|set(row.get('capabilities',[]))),
            'origins':sorted(set(old.get('origins',[]))|set(row.get('origins',[]))),
            'holders':sorted(set(old.get('holders',[]))|set(row.get('holders',[])))}
    return target


def shared_progress(path):
    """Read the compatibility map plus every Git-shared worker inventory."""
    path=Path(path);records={};workers=[]
    for worker in sorted((path.parent/'workers').glob('*.json')):
        saved=read_progress(worker)
        if saved.get('worker_id')!=worker.stem:raise ValueError(f'Worker inventory ID differs from filename: {worker}')
        workers.append(saved)
    worker_ids={worker['worker_id'] for worker in workers}
    if path.exists():
        legacy=read_progress(path)['records']
        # A current worker snapshot supersedes stale claims from the legacy
        # aggregate. Manually supplied claims from unknown workers still work.
        legacy={key:row for key,row in legacy.items()
            if not set(row.get('holders') or row.get('origins',[]))&worker_ids}
        merge_records(records,legacy)
    for saved in workers:merge_records(records,saved['records'])
    return records


def job_key(namespace, competition, season, mid):
    slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
    return f'{namespace}|{slug}|{season_label(season)}|{mid}'


class PortableProgress:
    def __init__(self, path, root, *, worker_id=None):
        self.path=Path(path) if path is not None else None;self.root=Path(root);self.scanning=False;self.skip_remote=True
        self.records=shared_progress(self.path) if self.path is not None else {}
        self.local_records={};self.previous_local={};self.worker_path=None
        self.origins=read_origins(self.root)
        if self.path is None:self.origin=read_worker_id(self.root);return
        identity=ensure_identity(self.root,worker_id)
        self.origin=identity['worker_id'];self.instance_id=identity['instance_id']
        self.worker_path=self.path.parent/'workers'/(self.origin+'.json')
        if self.worker_path.exists():
            saved=read_progress(self.worker_path)
            if saved.get('instance_id')!=self.instance_id:
                raise ValueError(f'Worker ID {self.origin!r} belongs to another data folder; choose a unique --worker-id')
            self.previous_local=saved['records']

    def done(self, namespace, competition, season, mid, required=()):
        row=self.records.get(job_key(namespace,competition,season,mid),{})
        capabilities=set(row.get('capabilities',[]))
        if 'browser_details' in capabilities:capabilities.add('sofascore_match_stats')
        return row.get('status')=='complete' and set(required)<=capabilities

    def record(self, namespace, competition, season, mid, capabilities=(), *, collected_here=False):
        key=job_key(namespace,competition,season,mid)
        old=self.local_records.get(key,{})
        origins=set(self.origins.get(key,[]))
        if not origins or collected_here:origins.add(self.origin)
        self.origins[key]=sorted(origins)
        row={'namespace':namespace,'competition':competition,'season':season_label(season),
            'match_id':str(mid),'status':'complete',
            'capabilities':sorted(set(old.get('capabilities',[]))|set(capabilities)),
            'origins':sorted(origins),
            'holders':[self.origin],
            'updated_at':datetime.now(timezone.utc).isoformat()}
        previous=self.previous_local.get(key,{})
        if all(previous.get(field)==value for field,value in row.items() if field!='updated_at'):
            row['updated_at']=previous['updated_at']
        self.local_records[key]=row
        merge_records(self.records,{key:row})
        if not self.scanning:self.save()

    def save(self):
        if self.path is None:return
        atomic_json(self.path,{'format':FORMAT,'records':self.records,
            'note':'Completed work only. Raw data and local completeness must be checked separately.'})
        atomic_json(self.worker_path,{'format':FORMAT,'worker_id':self.origin,'instance_id':self.instance_id,
            'records':dict(sorted(self.local_records.items())),
            'note':'Validated data held by this worker. origins identifies its original collectors.'},indent=2,sort_keys=True)
        save_origins(self.root,self.origins)

    def scan(self):
        self.local_records={}
        self.scanning=True
        try:self._scan()
        finally:self.scanning=False
        self.save()
        if self.path is not None:
            self.records=shared_progress(self.path)
            self.save()

    def inventory_summary(self, report=print):
        counts={}
        for row in self.local_records.values():
            group=(row['competition'],row['season'],row['namespace'])
            counts[group]=counts.get(group,0)+1
        report(f'Inventory for {self.origin}: {len(self.local_records)} validated local match records')
        for (competition,season,namespace),count in sorted(counts.items()):
            report(f'{competition} {season} | {namespace}: {count} local match records')
        report('Run review_data.py for percentages and resource completeness; shared claims do not count as local data.')

    def _scan(self):
        # Validate actual artifacts rather than trusting a copied status CSV.
        from review_data import who_valid
        from providers.cache import ResponseCache
        from .fotmobOnly import player_rows
        for marker in self.root.glob('*/*/match_checkpoints/*/CURRENT.json'):
            dataset=PandasStore(marker.parent).from_csv()
            for match in dataset.matches.values():
                directory=marker.parent.parent.parent
                slug=re.sub(r'[^a-z0-9]+','-',match.season.competition.name.lower()).strip('-')
                if (marker.parent.name!=str(match.id) or directory.parent.name!=slug
                        or directory.name!=season_label(match.season.name)):
                    raise ValueError(f'Checkpoint does not belong to its match/competition/season path: {marker}')
                self.record_match(match)
                relative=match.metadata.get('raw_response_files',{}).get('whoscored')
                mid=match.metadata.get('provider_match_ids',{}).get('whoscored')
                if relative and mid is not None:
                    from providers.identity import team_key
                    path=directory/relative
                    if path.exists():
                        saved=json.loads(path.read_text(encoding='utf-8'))
                        payload=saved.get('payload',{});centre=payload.get('matchCentreData',{})
                        if (saved.get('provider')!='whoscored' or saved.get('key')!=f'match/{mid}'
                                or not who_valid(payload,mid)
                                or centre.get('startDate','')[:10]!=match.kickoff.date().isoformat()
                                or any(team_key(centre.get(side,{}).get('name',''))!=team_key(getattr(match,side+'_team').name)
                                       for side in ('home','away'))):
                            raise ValueError(f'Invalid integrated WhoScored raw events: {path}')
                        self.record('whoscored_events',match.season.competition.name,match.season.name,mid)
        for subtree,namespace,provider in (('whoscored_events','whoscored_events','whoscored'),
                                          ('','whoscored_events','whoscored'),
                                          ('fotmob_player_stats','fotmob_players','fotmob')):
            pattern=f'{subtree}/*/*/schedule.json' if subtree else '*/*/whoscored_schedule.json'
            for schedule in self.root.glob(pattern):
                directory=schedule.parent
                job=directory/('whoscored_job.json' if not subtree else 'job.json')
                if not job.exists():continue
                settings=json.loads(job.read_text(encoding='utf-8'))
                competition=competition_by_name(settings['competition']).name;season=settings['season']
                slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
                if (settings.get('provider')!=provider or directory.parent.name!=slug
                        or directory.name!=season_label(season)):
                    raise ValueError(f'Provider collection namespace/selection mismatch: {directory}')
                cache=ResponseCache(directory/'raw')
                for event in json.loads(schedule.read_text(encoding='utf-8'))['events']:
                    mid=event['id'];path=cache.path(provider,f'match/{mid}')
                    if not path.exists():continue
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    if saved.get('provider')!=provider or saved.get('key')!=f'match/{mid}':
                        raise ValueError('Imported cache identity differs from its path')
                    payload=saved['payload']
                    if provider=='whoscored':
                        if not who_valid(payload,mid,event):raise ValueError(f'Invalid WhoScored raw events: {path}')
                    else:player_rows(payload,competition,season,event)
                    self.record(namespace,competition,season,mid)

    def record_match(self, match, *, collected_here=False):
        if match.metadata.get('id_namespace','sofascore')!='sofascore':return
        capabilities=['provider:'+key for key,value in match.metadata.get('provider_status',{}).items()
                      if value=='complete']
        from .completion import sofascore_stats_complete
        if sofascore_stats_complete(match):capabilities.append('sofascore_match_stats')
        if match.metadata.get('browser_details',{}).get('complete'):capabilities.append('browser_details')
        spatial=match.metadata.get('sofascore_spatial_status',{})
        terminal={'complete','unavailable','deferred'}
        for category in ('average_positions','shotmap'):
            if spatial.get(category) in terminal:capabilities.append('spatial:'+category)
        played=[p for lineup in match.lineups if lineup for p in lineup.players if p.did_play]
        for category in ('heatmap','rating_breakdown'):
            if played and all(spatial.get(str(p.player.id),{}).get(category) in terminal for p in played):
                capabilities.append('spatial:'+category)
        if capabilities:
            self.record('sofascore',match.season.competition.name,match.season.name,match.id,capabilities,
                        collected_here=collected_here)


def remote_done(namespace, competition, season, mid, required=()):
    progress=ACTIVE.get()
    return progress is not None and progress.skip_remote and progress.done(namespace,competition,season,mid,required)


def record_complete(namespace, competition, season, mid, *, collected_here=False):
    progress=ACTIVE.get()
    if progress is not None:progress.record(namespace,competition,season,mid,collected_here=collected_here)


def record_match(match, *, collected_here=True):
    progress=ACTIVE.get()
    if progress is not None:progress.record_match(match,collected_here=collected_here)


@contextmanager
def progress_session(progress):
    token=ACTIVE.set(progress)
    try:yield progress
    finally:ACTIVE.reset(token)

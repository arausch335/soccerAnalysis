"""Portable evidence of completed work; this never represents local raw data."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from copy import deepcopy

from storage.pandasStore import PandasStore
from storage.sharedFiles import read_json,write_json as atomic_json
from .selection import season_label, competition_by_name
from .workerIdentity import ensure_identity, read_worker_id, read_origins, save_origins

FORMAT = 'soccer-analysis-progress-v1'
ACTIVE = ContextVar('ensemble_progress', default=None)


def read_progress(path):
    saved=read_json(path)
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


def shared_progress(path, *, local_worker=None,trusted=False):
    """Read local holdings and the inventories received from Main."""
    path=Path(path);records={};workers=[]
    from .reporting import inventory_paths
    paths=inventory_paths(path.parent,local_worker=local_worker)
    if trusted:paths=[p for p in paths if p.stem==local_worker or p.parent==path.parent/'reporting/workers']
    for worker in paths:
        saved=read_progress(worker)
        if saved.get('worker_id')!=worker.stem:raise ValueError(f'Worker inventory ID differs from filename: {worker}')
        workers.append(saved)
    worker_ids={worker['worker_id'] for worker in workers}
    if path.exists() and not trusted:
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
        self.path=Path(path) if path is not None else None;self.root=Path(root);self.scanning=False;self.skip_remote=True;self.trusted_only=False
        self.records=shared_progress(self.path,local_worker=worker_id or read_worker_id(root)) if self.path is not None else {}
        self.local_records={};self.previous_local={};self.worker_path=None
        self._capture=None;self._inventory_cache=None;self.scan_summary={}
        self._dirty=set();self._replace_seasons=None;self.scan_seasons=None
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

    def record(self, namespace, competition, season, mid, capabilities=(), *, collected_here=False, resources=None, provider_match_ids=None,evidence=None,status='complete'):
        key=job_key(namespace,competition,season,mid)
        old=self.local_records.get(key,{})
        origins=set(self.origins.get(key,[]))
        if not origins or collected_here:origins.add(self.origin)
        self.origins[key]=sorted(origins)
        row={'namespace':namespace,'competition':competition,'season':season_label(season),
            'match_id':str(mid),'status':status,
            'capabilities':sorted(set(old.get('capabilities',[]))|set(capabilities)),
            'origins':sorted(origins),
            'holders':[self.origin],
            'updated_at':datetime.now(timezone.utc).isoformat()}
        native={'whoscored_events':'whoscored_events','fotmob_players':'fotmob_stats'}.get(namespace)
        row['resources']={**old.get('resources',{}),**(resources or ({native:'complete'} if native else {}))}
        if provider_match_ids:row['provider_match_ids']={str(k):str(v) for k,v in provider_match_ids.items() if v is not None}
        if evidence:row['evidence']=evidence
        prior={**self.previous_local.get(key,{}),**old}
        owners={}
        for name,state in row['resources'].items():
            known=prior.get('resource_origins',{}).get(name)
            if collected_here and state=='complete' and prior.get('resources',{}).get(name)!='complete':known=[self.origin]
            owners[name]=known or sorted(origins)
        row['resource_origins']=owners
        previous=self.previous_local.get(key,{})
        if all(previous.get(field)==value for field,value in row.items() if field!='updated_at'):
            row['updated_at']=previous['updated_at']
        self._dirty.add(key)
        self.local_records[key]=row
        if self._capture is not None:self._capture[key]=deepcopy(row)
        merge_records(self.records,{key:row})
        if not self.scanning:self.save()

    def save(self):
        if self.path is None:return
        from filelock import FileLock
        self.worker_path.parent.mkdir(parents=True,exist_ok=True)
        with FileLock(str(self.worker_path.with_suffix('.lock'))):
            saved=read_progress(self.worker_path) if self.worker_path.exists() else {}
            existing=saved.get('records',{});schedules=saved.get('schedules',{})
            if self._replace_seasons is not None:
                existing={key:row for key,row in existing.items() if self.record_season(key) not in self._replace_seasons}
                schedules={key:row for key,row in schedules.items() if key not in self._replace_seasons}
                schedules.update(self._schedule_updates)
            # A foreground session writes only matches it has just validated.
            # It cannot erase background indexing of an unrelated season.
            existing.update({key:self.local_records[key] for key in self._dirty})
            self.local_records=existing
            atomic_json(self.worker_path,{'format':FORMAT,'worker_id':self.origin,'instance_id':self.instance_id,
                'records':dict(sorted(existing.items())),'schedules':schedules,
                'note':'Validated data held by this worker. origins identifies its original collectors.'},indent=2,sort_keys=True)
            self._dirty.clear()
        merge_records(self.records,existing)
        atomic_json(self.path,{'format':FORMAT,'records':self.records,
            'note':'Completed work only. Raw data and local completeness must be checked separately.'})
        save_origins(self.root,self.origins)

    @staticmethod
    def record_season(key):
        parts=key.split('|')
        return '/'.join(parts[1:3])

    def scan(self,*,full=False,cache=True,cancel=None,seasons=None):
        from .inventoryCache import InventoryCache
        self.origins=read_origins(self.root)
        self._scope_schedules={}
        self.scan_seasons=set(seasons) if seasons is not None else None
        self._dirty=set()
        self.local_records={}
        if self.path is None:self.records={}
        if self.worker_path and self.worker_path.exists():
            self.previous_local=read_progress(self.worker_path)['records']
        receipts=InventoryCache(self.root,self.origin,full=full) if cache else None
        self.scanning=True
        self._inventory_cache=receipts
        self._scan_cancel=cancel
        try:
            self._scan()
            if receipts and seasons is None:receipts.prune()
            self.scan_summary={'reused':receipts.hits if receipts else 0,
                               'validated':receipts.checked if receipts else len(self.local_records)}
        finally:
            self.scanning=False;self._capture=None;self._inventory_cache=None
            if receipts:receipts.close()
        self._schedule_updates={}
        if self.path is not None:
            for schedule in self.root.glob('*/*/schedule.json'):
                key=schedule.parent.relative_to(self.root).as_posix()
                if self.scan_seasons is not None and key not in self.scan_seasons:continue
                self._check_cancelled()
                settings=schedule.parent/'job.json'
                if not settings.exists():settings=schedule.parent/'config.json'
                if not settings.exists():continue
                selection=json.loads(settings.read_text(encoding='utf-8'))
                # Legacy markers may name only their baseline provider. Do not
                # invent target IDs or a competition/season from incomplete metadata.
                if not selection.get('competition') or not selection.get('season'):continue
                scope=schedule.parent/'fixture-scopes.json'
                events=json.loads((scope if scope.exists() else schedule).read_text(encoding='utf-8')).get('events',[])
                from .selection import event_competition
                competition=competition_by_name(selection['competition']).name
                groups={competition:[]}
                for event in events:groups.setdefault(event_competition(competition,event),[]).append(str(event['id']))
                for name,ids in groups.items():
                    slug=re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')
                    self._schedule_updates[slug+'/'+season_label(selection['season'])]={'competition':name,
                        'season':season_label(selection['season']),'kind':'combined','scope_version':2,'match_ids':sorted(set(ids))}
                attempts=schedule.parent/'collection.csv'
                if attempts.is_file():
                    import csv
                    events_by_id={str(e['id']):e for e in events}
                    with attempts.open(encoding='utf-8',newline='') as stream:
                        for attempt in csv.DictReader(stream):
                            event=events_by_id.get(str(attempt.get('match_id')))
                            if event and attempt.get('status')=='failed':
                                name=event_competition(competition,event)
                                key=job_key('sofascore',name,selection['season'],event['id'])
                                if key not in self.local_records:record_failure(name,selection['season'],event,attempt,progress=self)
        self._replace_seasons=(self.scan_seasons if self.scan_seasons is not None else
                               {self.record_season(key) for key in set(self.previous_local)|set(self.local_records)})
        self._dirty.update(self.local_records)
        self.save()
        self._replace_seasons=None
        if self.path is not None:
            self.records=shared_progress(self.path,local_worker=self.origin,trusted=self.trusted_only)
            self.save()

    def _restore(self,anchor,context=''):
        cache=self._inventory_cache
        if cache is None:return False
        rows=cache.restore(anchor,context)
        if rows is None:return False
        for key,saved in rows.items():
            row=deepcopy(saved)
            row['origins']=sorted(set(row['origins'])|set(self.origins.get(key,[])))
            self.origins[key]=row['origins'];self.local_records[key]=row
            self._dirty.add(key)
            merge_records(self.records,{key:row})
        return True

    def inventory_summary(self, report=print):
        counts={}
        for row in self.local_records.values():
            group=(row['competition'],row['season'],row['namespace'])
            counts[group]=counts.get(group,0)+1
        report(f'Inventory for {self.origin}: {len(self.local_records)} validated local match records')
        for (competition,season,namespace),count in sorted(counts.items()):
            report(f'{competition} {season} | {namespace}: {count} local match records')
        report('Run review_data.py for percentages and resource completeness; shared claims do not count as local data.')
        report(f'Inventory checks: {self.scan_summary.get("reused",0)} unchanged artifacts reused; '
               f'{self.scan_summary.get("validated",0)} new/changed artifacts validated.')

    def _scan(self):
        # Validate actual artifacts rather than trusting a copied status CSV.
        from review_data import who_valid
        from providers.cache import ResponseCache
        from .legacyFotmobRows import player_rows
        for marker in self.root.glob('*/*/match_checkpoints/*/CURRENT.json'):
            if self.scan_seasons is not None and '/'.join(marker.relative_to(self.root).parts[:2]) not in self.scan_seasons:continue
            self._check_cancelled()
            if self._restore(marker):continue
            store=PandasStore(marker.parent)
            from storage.pandasStore import SCHEMA
            folder=store.current_directory
            paths=[marker,*[folder/(name+'.csv') for name in SCHEMA]]
            season_folder=marker.parent.parent.parent
            scope=season_folder/'fixture-scopes.json'
            paths.append(scope if scope.exists() else season_folder/'schedule.json')
            if season_folder.parent.name in ('champions-league','europa-league'):
                paths.append(self.root/(season_folder.parent.name+'-qualifiers')/season_folder.name/'match_checkpoints'/marker.parent.name/'CURRENT.json')
            cache=self._inventory_cache
            dependencies=cache.dependencies(paths) if cache else {}
            dataset=store.from_csv()
            for match in dataset.matches.values():
                directory=marker.parent.parent.parent
                relative=match.metadata.get('raw_response_files',{}).get('whoscored')
                if relative:paths.append(directory/relative)
            if cache:
                dependencies.update(cache.dependencies(paths[len(SCHEMA)+1:]))
            self._capture={}
            for match in dataset.matches.values():
                directory=marker.parent.parent.parent
                slug=re.sub(r'[^a-z0-9]+','-',match.season.competition.name.lower()).strip('-')
                if (marker.parent.name!=str(match.id) or directory.parent.name!=slug
                        or directory.name!=season_label(match.season.name)):
                    raise ValueError(f'Checkpoint does not belong to its match/competition/season path: {marker}')
                self.record_match(match)
            if cache:cache.remember(marker,dependencies,self._capture)
            self._capture=None
        for subtree,namespace,provider in (('whoscored_events','whoscored_events','whoscored'),
                                          ('whoscored','whoscored_events','whoscored'),
                                          ('','whoscored_events','whoscored'),
                                          ('fotmob_player_stats','fotmob_players','fotmob')):
            pattern=f'{subtree}/*/*/schedule.json' if subtree else '*/*/whoscored_schedule.json'
            patterns=(pattern,f'{subtree}/*/*/whoscored_schedule.json') if provider=='whoscored' and subtree else (pattern,)
            for schedule in sorted({path for pattern in patterns for path in self.root.glob(pattern)}):
                directory=schedule.parent
                if self.scan_seasons is not None and '/'.join(directory.parts[-2:]) not in self.scan_seasons:continue
                from .whoScoredData import metadata_path
                job=metadata_path(directory,'job.json') if provider=='whoscored' else directory/'job.json'
                if not job.exists():continue
                settings=json.loads(job.read_text(encoding='utf-8'))
                competition=competition_by_name(settings['competition']).name;season=settings['season']
                slug=re.sub(r'[^a-z0-9]+','-',competition.lower()).strip('-')
                if (settings.get('provider')!=provider or directory.parent.name!=slug
                        or directory.name!=season_label(season)):
                    raise ValueError(f'Provider collection namespace/selection mismatch: {directory}')
                cache=ResponseCache(directory/'raw')
                for event in json.loads(schedule.read_text(encoding='utf-8'))['events']:
                    self._check_cancelled()
                    mid=event['id'];path=cache.path(provider,f'match/{mid}')
                    if not path.exists():continue
                    context=json.dumps([namespace,competition,season,event],sort_keys=True)
                    if self._restore(path,context):continue
                    receipts=self._inventory_cache
                    dependencies=receipts.dependencies([path]) if receipts else {}
                    self._capture={}
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    if saved.get('provider')!=provider or saved.get('key')!=f'match/{mid}':
                        raise ValueError('Imported cache identity differs from its path')
                    payload=saved['payload']
                    if provider=='whoscored':
                        if not who_valid(payload,mid,event):raise ValueError(f'Invalid WhoScored raw events: {path}')
                    else:player_rows(payload,competition,season,event)
                    self.record(namespace,competition,season,mid)
                    if receipts:receipts.remember(path,dependencies,self._capture,context)
                    self._capture=None

    def _check_cancelled(self):
        if self._scan_cancel is not None and self._scan_cancel.is_set():
            from .inventoryCache import InventoryInterrupted
            raise InventoryInterrupted()

    def _integrated_events(self,match,collected_here,competition=None):
        from .whoScoredData import integrated_error
        relative=match.metadata.get('raw_response_files',{}).get('whoscored')
        mid=match.metadata.get('provider_match_ids',{}).get('whoscored')
        if relative and mid is not None:
            directory=self.root/re.sub(r'[^a-z0-9]+','-',match.season.competition.name.lower()).strip('-')/season_label(match.season.name)
            path=directory/relative
            if path.exists():
                saved=json.loads(path.read_text(encoding='utf-8'))
                reason=('cache provider or key differs from the checkpoint'
                        if saved.get('provider')!='whoscored' or saved.get('key')!=f'match/{mid}' else
                        integrated_error(match,saved.get('payload',{}),root=self.root))
                if reason:
                    if self.scanning:raise ValueError(f'Invalid integrated WhoScored raw events: {path} ({reason})')
                    return
                self.record('whoscored_events',competition or match.season.competition.name,match.season.name,mid,collected_here=False)

    def record_match(self, match, *, collected_here=False):
        if match.metadata.get('id_namespace','sofascore')!='sofascore':return
        competition=match.season.competition.name
        evidence=dict(match.metadata.get('collection_evidence',{}))
        evidence.setdefault('label',match.home_team.name+' vs '+match.away_team.name)
        evidence.setdefault('date',match.kickoff.date().isoformat())
        from providers.matchComparison import fixture_summary
        if evidence.get('error') or match.metadata.get('provider_comparisons'):
            event=match.metadata.get('event')
            if event:evidence['baseline_fixture']=fixture_summary(event,'sofascore')
            evidence['provider_comparisons']=match.metadata.get('provider_comparisons',{})
            evidence['identity_mismatches']={provider:rows[:16] for provider,rows in match.metadata.get('identity_mismatches',{}).items()
                if rows and match.metadata.get('provider_status',{}).get(provider)!='complete'}
        if competition in ('Champions League','Europa League'):
            from .selection import event_competition
            directory=self.root/re.sub('[^a-z0-9]+','-',competition.lower()).strip('-')/season_label(match.season.name)
            cache=getattr(self,'_scope_schedules',{})
            if directory not in cache:
                path=directory/'fixture-scopes.json'
                if not path.exists():path=directory/'schedule.json'
                cache[directory]={str(e['id']):e for e in json.loads(path.read_text(encoding='utf-8')).get('events',[])} if path.exists() else {}
                self._scope_schedules=cache
            event=cache[directory].get(str(match.id),{})
            competition=event_competition(competition,event)
            evidence['qualifying']=competition.endswith(' Qualifiers')
            if event:
                evidence.setdefault('label',event['homeTeam']['name']+' vs '+event['awayTeam']['name'])
            # Once a canonical qualifier copy exists its newer checkpoint owns
            # the audit. The retained legacy source must not overwrite it.
            if competition!=match.season.competition.name:
                canonical=self.root/re.sub('[^a-z0-9]+','-',competition.lower()).strip('-')/season_label(match.season.name)
                if (canonical/'match_checkpoints'/str(match.id)/'CURRENT.json').exists():return
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
        resources={name:'not_attempted' for name in ('sofascore_stats','sofascore_details','match_statistics','incidents',
            'momentum','average_positions','heatmaps','shotmaps','action_maps','whoscored_stats','fotmob_enrichment')}
        if 'sofascore_match_stats' in capabilities:resources['sofascore_stats']='complete'
        detail=match.metadata.get('browser_details',{})
        coverage=detail.get('coverage',{})
        for category,name in (('statistics','match_statistics'),('incidents','incidents'),('momentum','momentum')):
            if category in coverage:resources[name]=coverage[category]
        detail_states=[coverage.get(key,'unknown') for key in ('statistics','incidents','momentum')]
        if coverage:
            resources['sofascore_details']=detail_states[0] if len(set(detail_states))==1 else 'partial'
            player_states=[v for key,v in coverage.items() if key.startswith('player/') and key.endswith('/statistics')]
            if player_states:resources['sofascore_stats']=player_states[0] if len(set(player_states))==1 else 'partial'
        for category,name in (('average_positions','average_positions'),('shotmap','shotmaps')):
            if category in spatial:resources[name]=spatial[category]
        for category,name in (('heatmap','heatmaps'),('rating_breakdown','action_maps')):
            states=[spatial.get(str(p.player.id),{}).get(category,'not_attempted') for p in played]
            if states:resources[name]=states[0] if len(set(states))==1 else 'partial'
        for provider,name in (('whoscored','whoscored_stats'),('fotmob','fotmob_enrichment')):
            state=match.metadata.get('provider_status',{}).get(provider)
            if state:resources[name]=state
        if capabilities:
            self.record('sofascore',competition,match.season.name,match.id,capabilities,
                        collected_here=collected_here,resources=resources,provider_match_ids=match.metadata.get('provider_match_ids',{}),evidence=evidence)
        self._integrated_events(match,collected_here,competition)


def remote_done(namespace, competition, season, mid, required=()):
    progress=ACTIVE.get()
    if progress is None or not progress.skip_remote:return False
    if progress.trusted_only:
        from .workInstructions import approved_by_main
        return approved_by_main(progress,job_key(namespace,competition,season,mid),required)
    row=progress.records.get(job_key(namespace,competition,season,mid),{})
    holders=set(row.get('holders') or row.get('origins',[]))
    # A previous local inventory is not remote evidence. In particular, missing
    # local checkpoints must be recollected rather than skipped at startup.
    if holders=={progress.origin}:return False
    return progress.done(namespace,competition,season,mid,required)


def record_complete(namespace, competition, season, mid, *, collected_here=False):
    progress=ACTIVE.get()
    if progress is not None:progress.record(namespace,competition,season,mid,collected_here=collected_here)


def record_match(match, *, collected_here=True):
    progress=ACTIVE.get()
    if progress is not None:progress.record_match(match,collected_here=collected_here)


def record_failure(competition,season,event,entry,*,progress=None):
    progress=progress or ACTIVE.get()
    if progress is None:return
    from .reporting import redact
    resources={name:'failed' for name in ('sofascore_stats','sofascore_details','match_statistics','incidents','momentum',
        'average_positions','heatmaps','shotmaps','action_maps','fotmob_enrichment','whoscored_stats')}
    progress.record('sofascore',competition,season,event['id'],resources=resources,status='failed',
        evidence={'attempts':int(entry.get('attempts') or 0),'error':redact(entry.get('error',''))[:2000],
                  'label':event['homeTeam']['name']+' vs '+event['awayTeam']['name'],
                  'date':datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat() if event.get('startTimestamp') else None})


@contextmanager
def progress_session(progress):
    token=ACTIVE.set(progress)
    try:yield progress
    finally:ACTIVE.reset(token)

"""Keep Main's local holdings current without running a collector."""
from datetime import datetime, timezone
from pathlib import Path
import threading

from filelock import FileLock, Timeout
from storage.layout import system_path
from .ensembleProgress import PortableProgress,read_progress
from .inventoryCache import InventoryInterrupted


class InventoryMonitor:
    def __init__(self,root,ensemble,worker,*,period=30,on_change=None,report=print):
        self.root=Path(root);self.ensemble=Path(ensemble);self.worker=worker
        self.period=period;self.on_change=on_change;self.report=report
        self.stop=threading.Event();self.status={'state':'waiting'};self.thread=None
        self.error=None;self.first=True

    def check(self):
        try:
            from .maintenance import background, DeferredWork
            self.status={**self.status,'state':'checking'}
            progress=PortableProgress(self.ensemble/'provenance.json',self.root,worker_id=self.worker)
            before=read_progress(progress.worker_path) if progress.worker_path.exists() else {}
            previous=before.get('records',{})
            seasons={progress.record_season(key) for key in previous}
            for marker in self.root.glob('*/*/match_checkpoints/*/CURRENT.json'):
                seasons.add('/'.join(marker.relative_to(self.root).parts[:2]))
            for pattern in ('*/*/job.json','*/*/whoscored_job.json','fotmob_player_stats/*/*/job.json',
                            'whoscored_events/*/*/job.json','whoscored/*/*/job.json'):
                for job in self.root.glob(pattern):seasons.add('/'.join(job.parent.parts[-2:]))
            # Frozen completion remains authoritative until Main explicitly reopens it.
            from .jobCompletion import CompletionStore
            import re
            from config.workerPlan import load_plan
            from .jobCompletion import configuration
            plan=load_plan()
            active={configuration(job) for job in plan.jobs}
            completed=CompletionStore(self.root).snapshot().get('completed',{})
            frozen={re.sub('[^a-z0-9]+','-',v['competition'].lower()).strip('-')+'/'+v['season'] for k,v in completed.items() if k in active}
            command_path=self.root/'_system/workers/main-instructions.json'
            if command_path.exists():
                import json
                from .storageAgent import season_for
                saved=json.loads(command_path.read_text(encoding='utf-8'));commands=saved.get('jobs',{}) if saved.get('plan_revision')==plan.revision else {}
                for job in plan.for_worker(self.worker):
                    if commands.get(job.id,{}).get('done'):frozen.add(season_for(job))
            seasons-=frozen
            reused=validated=waiting=0;errors=[]
            for season in sorted(seasons):
                if self.stop.is_set():return False
                try:
                    with background(self.root,season,self.stop) as permit:
                        try:
                            progress.scan(cancel=permit,seasons={season})
                            reused+=progress.scan_summary['reused'];validated+=progress.scan_summary['validated']
                        except (DeferredWork,InventoryInterrupted):raise
                        except Exception as exc:
                            # Withdraw unverified holdings before releasing this
                            # season. Main must not keep authorizing stale skips.
                            saved=read_progress(progress.worker_path) if progress.worker_path.exists() else {}
                            progress._replace_seasons={season};progress._dirty=set()
                            progress._schedule_updates={key:value for key,value in saved.get('schedules',{}).items() if key==season}
                            progress.save();progress._replace_seasons=None
                            errors.append(season+': '+str(exc))
                except (Timeout,DeferredWork,InventoryInterrupted):waiting+=1
            after=read_progress(progress.worker_path) if progress.worker_path.exists() else {}
            current=after.get('records',{})
            changed=previous!=current or before.get('schedules',{})!=after.get('schedules',{})
            self.status={'state':'attention' if errors else 'current','checked_at':datetime.now(timezone.utc).isoformat(),
                         'reused':reused,'validated':validated,'records':len(current),'seasons_waiting':waiting}
            if self.first or validated:
                self.report(f'[Inventory] {self.worker}: {reused} unchanged artifacts reused; '
                            f'{validated} new/changed artifacts validated; {waiting} busy seasons deferred.',flush=True)
            message='; '.join(errors)
            if message and message!=self.error:self.report('[Inventory] '+self.worker+' needs review: '+message,flush=True)
            if message:self.status['message']=message
            self.first=False;self.error=message or None
            if changed:
                if self.on_change:self.on_change()
                from providers.identityReview import seed_existing
                seed_existing(self.root)
            return changed
        except InventoryInterrupted:pass
        except Exception as exc:
            message=str(exc)
            self.status={**self.status,'state':'attention','message':message}
            if message!=self.error:self.report('[Inventory] '+self.worker+' needs review: '+message,flush=True)
            self.error=message
        return False

    def start(self):
        def watch():
            while not self.stop.is_set():
                self.check();self.stop.wait(self.period)
        self.thread=threading.Thread(target=watch,name='inventory-monitor',daemon=True)
        self.thread.start();return self

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=15)

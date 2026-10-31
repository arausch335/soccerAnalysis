"""Independent fixture heartbeats carried by the existing durable worker reporter."""
from contextlib import ExitStack
from datetime import datetime,timezone
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from storage.layout import system_path
from storage.sharedFiles import write_json

FORMAT='soccer-fixture-runtime-v1'
ACTIVE={'starting','collecting','matching','exporting','delivering','refreshing_ids','waiting_for_extension'}


def now():return datetime.now(timezone.utc).isoformat()


def path_for(root,worker):return system_path(root,'worker_runtime')/'fixtures'/(worker+'.json')


def validate(value,worker):
    if (not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('worker_id')!=worker
            or value.get('state') not in ACTIVE|{'finished','needs_attention','stopped','failed'}
            or not isinstance(value.get('run_id'),str) or len(value['run_id'])>128):
        raise ValueError('Invalid fixture runtime identity/state')
    for field in ('updated_at','heartbeat_at','started_at'):
        stamp=datetime.fromisoformat(value[field])
        if stamp.tzinfo is None:raise ValueError('Fixture timestamp needs a timezone')
    for field in ('total_seasons','completed_seasons','total_schedules','checked_schedules',
                  'complete_schedules','partial_schedules','missing_schedules','cached_schedules','baseline_fixtures'):
        if type(value.get(field)) is not int or value[field]<0:raise ValueError('Invalid fixture progress count')
    if type(value.get('deferred_schedules',0)) is not int or value.get('deferred_schedules',0)<0:
        raise ValueError('Invalid deferred fixture progress count')
    return value


class Progress:
    def __init__(self,root,ensemble,total,*,network=True,enabled=True,heartbeat=10):
        from collection.workerIdentity import read_worker_id
        self.root=Path(root);self.ensemble=Path(ensemble);self.worker=read_worker_id(root)
        self.path=path_for(root,self.worker);self.network=network;self.enabled=enabled;self.heartbeat=heartbeat
        self.lock=threading.RLock();self.last_saved=0;self.reporter=None;self.storage=None;self.stack=ExitStack();self.stop=threading.Event();self.thread=None
        self.value=dict(format=FORMAT,worker_id=self.worker,run_id=uuid4().hex,process_id=os.getpid(),
            state='starting',started_at=now(),updated_at=now(),heartbeat_at=now(),
            total_seasons=total,completed_seasons=0,total_schedules=total*3,checked_schedules=0,
            complete_schedules=0,partial_schedules=0,missing_schedules=0,deferred_schedules=0,cached_schedules=0,baseline_fixtures=0,
            competition='',season='',provider='',current_fixtures=0,stage='',month='',delivery={})

    def __enter__(self):
        if not self.enabled:return self
        from collection.reporting import Reporter,load_local,capture_logs
        options=load_local(self.root)
        identity=system_path(self.root,'identity.json')
        if identity.exists():
            options=options or {'worker_id':self.worker,'url':None,'token':''}
            if options.get('mode')=='server':
                options={'worker_id':self.worker,'url':f"http://{options['bind']}:{options['port']}",
                         'token':options['tokens'][self.worker]}
            if not self.network:options={**options,'url':None}
            self.reporter=Reporter(self.root,self.ensemble,self.worker,options,self.heartbeat)
            if self.network and self.reporter.url:
                try:self.reporter.pull_registry()
                except (OSError,ValueError,EOFError):
                    message='[Fixtures] Main registry download unavailable; continuing with saved local IDs. The next run will retry.'
                    print(message,flush=True);self.reporter.log(message,'WARNING')
            from concordance.sync import installed_version
            self.value['main_registry_version']=installed_version(self.root)
            self.stack.callback(lambda:self.reporter.close(grace=1))
            self.stack.enter_context(capture_logs(self.reporter))
            self.reporter.start(snapshot=self.publish)
        else:self.publish()
        if not self.reporter or not self.reporter.url:
            def heartbeat():
                while not self.stop.wait(self.heartbeat):self.publish()
            self.thread=threading.Thread(target=heartbeat,name='local-fixture-heartbeat',daemon=True)
            self.thread.start()
        if self.network:
            from collection.storageConfig import path_for as storage_path
            import json
            setup=storage_path(self.root)
            if setup.is_file() and json.loads(setup.read_text(encoding='utf-8')).get('role')=='producer':
                import config
                from config.workerPlan import load_plan
                from collection.storageAgent import StorageAgent
                settings=load_plan().storage
                if settings['enabled']:
                    self.storage=StorageAgent(self.root,self.worker,settings,dataset_root=config.PROJECT_ROOT/'dataset').start()
                    self.stack.callback(self.storage.close)
        return self

    def publish(self,**_):
        if not self.enabled:return
        with self.lock:
            if self.storage:self.value['raw_delivery']=dict(self.storage.status)
            self.value['heartbeat_at']=now()
            write_json(self.path,self.value)
            self.last_saved=time.monotonic()
            if self.reporter:self.reporter.enqueue_fixture(dict(self.value))

    def __call__(self,event,**values):
        with self.lock:
            self.value.update(values)
            if event=='season_started':
                self.value.update(state='collecting',provider='',stage='',month='',current_fixtures=0)
            elif event=='provider_started':self.value.update(state='collecting',stage='',month='',current_fixtures=0)
            elif event=='provider_finished':
                self.value['checked_schedules']+=1
                status=values.get('status')
                counter={'complete':'complete_schedules','partial':'partial_schedules','deferred':'deferred_schedules'}.get(status,'missing_schedules')
                self.value[counter]+=1
                if values.get('source')=='checkpoint':self.value['cached_schedules']+=1
                if values.get('provider')=='sofascore':self.value['baseline_fixtures']+=values.get('current_fixtures',0)
            elif event=='season_finished':self.value['completed_seasons']+=1
            elif event in ('matching','exporting','delivering','refreshing_ids'):self.value.update(state=event,provider='',stage='',month='')
            elif event in ('finished','needs_attention','stopped','failed'):self.value.update(state=event,ended_at=now())
            if event=='provider_finished' and values.get('status') not in ('complete','deferred'):
                self.value['last_issue']={k:self.value.get(k,'') for k in ('competition','season','provider','status')}
            self.value['event']=event;self.value['updated_at']=now()
            if event!='provider_progress':
                self.value['activity']=(self.value.get('activity',[])+[{'at':now(),'event':event,
                    **{k:self.value.get(k,'') for k in ('competition','season','provider','status')}}])[-30:]
                if self.reporter:
                    label=' '.join(str(self.value.get(k,'')) for k in ('competition','season','provider'))
                    self.reporter.log('[Fixtures] '+label+' · '+event.replace('_',' ')+
                        (' · '+str(values['status']) if event=='provider_finished' else ''),
                        'ERROR' if event=='failed' else 'WARNING' if event=='needs_attention' or event=='provider_finished' and values.get('status') not in ('complete','deferred') else 'INFO')
            if event!='provider_progress' or time.monotonic()-self.last_saved>=1:self.publish()

    def __exit__(self,kind,error,tb):
        if kind:
            self('stopped' if issubclass(kind,KeyboardInterrupt) else 'failed')
            if self.reporter and not issubclass(kind,KeyboardInterrupt):
                self.reporter.log('[Fixtures] '+kind.__name__+': '+str(error),'ERROR')
        self.stop.set()
        if self.thread:self.thread.join(timeout=1)
        self.publish()
        # Leave a durable terminal event even when Main is offline. No network
        # drain here: stopping must not depend on Main's availability.
        self.stack.close()

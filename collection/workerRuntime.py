"""Small, portable status snapshots. Never include paths, cookies, URLs or errors."""
from storage.layout import system_path
from datetime import datetime, timezone
import json
from pathlib import Path

from storage.pandasStore import atomic_json


def now():return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(self,root,ensemble,worker):
        self.path=system_path(Path(root),'worker_runtime')/(worker+'.json')
        self.public=Path(ensemble)/'runtime'/(worker+'.json')
        previous=json.loads(self.path.read_text()) if self.path.exists() else {}
        self._saved_payload=None
        self.value={'format':'soccer-worker-runtime-v1','worker_id':worker,'state':'starting',
                    'jobs':previous.get('jobs',{}),'activity':previous.get('activity',[])[-40:],
                    'schedules':previous.get('schedules',{}),
                    **{key:previous[key] for key in ('job','job_started_at','job_ended_at','phase_stats') if key in previous}}

    def save(self,**values):
        self.value.update(values)
        payload=json.dumps({k:v for k,v in self.value.items() if k!='updated_at'},sort_keys=True)
        if payload==self._saved_payload:return
        self._saved_payload=payload;self.value['updated_at']=now()
        atomic_json(self.path,self.value);atomic_json(self.public,self.value)

    def begin_job(self,job):
        self.save(state='scraping',job=job,job_started_at=now(),job_ended_at=None,phase_stats={},current=None,current_provider=None,provider_active=False)

    def event(self,value):
        value=dict(value)
        selected=value.pop('selected_match_ids',None)
        key='|'.join(str(value.get(k,'')) for k in ('competition','season','kind'))
        if selected is not None:
            self.value.setdefault('schedules',{})[key]={'competition':value['competition'],'season':value['season'],
                'kind':value['kind'],'match_ids':selected}
        self.value.setdefault('phase_stats',{})[key]=value
        if value.get('provider'):
            self.value['current_provider']=value['provider']
            self.value['provider_active']=value.get('provider_active',False)
        self.value['activity']=(self.value['activity']+[{**value,'at':now()}])[-40:]
        self.save(current=value)


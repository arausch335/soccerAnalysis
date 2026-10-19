"""Small, portable status snapshots. Never include paths, cookies, URLs or errors."""
from datetime import datetime, timezone
import json
from pathlib import Path

from storage.pandasStore import atomic_json


def now():return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(self,root,ensemble,worker):
        self.path=Path(root)/'worker_runtime'/(worker+'.json')
        self.public=Path(ensemble)/'runtime'/(worker+'.json')
        previous=json.loads(self.path.read_text()) if self.path.exists() else {}
        self._saved_payload=None
        self.value={'format':'soccer-worker-runtime-v1','worker_id':worker,'state':'starting',
                    'jobs':previous.get('jobs',{}),'activity':previous.get('activity',[])[-40:]}

    def save(self,**values):
        self.value.update(values)
        payload=json.dumps({k:v for k,v in self.value.items() if k!='updated_at'},sort_keys=True)
        if payload==self._saved_payload:return
        self._saved_payload=payload;self.value['updated_at']=now()
        atomic_json(self.path,self.value);atomic_json(self.public,self.value)

    def event(self,value):
        self.value['activity']=(self.value['activity']+[{**value,'at':now()}])[-40:]
        self.save(current=value)


"""Bounded, compressed scratch responses; season raw files are authoritative."""
from contextlib import contextmanager
from contextvars import ContextVar
import gzip
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from storage.pandasStore import atomic_replace
from storage.layout import system_path

_CAPTURE = ContextVar('request_cache_capture', default=None)
MAX_BYTES = 256 * 1024 * 1024
_LAST_TRIM = {}

@contextmanager
def saved_response_batch():
    """Delete scratch responses only after the caller has committed its raw file."""
    paths=set();token=_CAPTURE.set(paths)
    try:
        yield
    except BaseException:
        raise  # An interrupted/failed raw save keeps scratch responses for resume.
    else:
        for path in paths:
            try:path.unlink(missing_ok=True)
            except OSError:pass
    finally:_CAPTURE.reset(token)

class RequestCache:
    def __init__(self,root,provider):
        self.root=Path(root);self.provider=provider
        self.directory=system_path(self.root,'cache')/'responses'/provider
        self.used={}
    def identity(self,endpoint,params,kind):return json.dumps([self.provider,endpoint,params,kind],sort_keys=True)
    def path(self,endpoint,params,kind):
        return self.directory/(hashlib.sha256(self.identity(endpoint,params,kind).encode()).hexdigest()+'.json.gz')
    def _touch(self,path):
        paths=_CAPTURE.get()
        if paths is not None:paths.add(path)
    def read(self,endpoint,params=None,kind='json'):
        path=self.path(endpoint,params,kind)
        if not path.exists():
            path=self.directory.parent.parent/'recovered'/self.provider/path.name
            if not path.exists():return self._read_retired(endpoint,params,kind)
        with gzip.open(path,'rt',encoding='utf-8') as f:value=json.load(f)
        if value['identity']!=self.identity(endpoint,params,kind):raise ValueError('Response cache identity mismatch')
        self.used[path]=(endpoint,params);self._touch(path);return value
    def _read_retired(self,endpoint,params,kind):
        bases={'sofascore':('https://api.sofascore.com/api/v1','https://www.sofascore.com/api/v1'),
               'fotmob':('https://www.fotmob.com/api/data',),'whoscored':('https://www.whoscored.com',)}
        candidates=[]
        for parent in (self.root,self.root/'_system'/'retired_cache'):
            for base in bases[self.provider]:
                old_identity=json.dumps([base,endpoint,params,kind],sort_keys=True)
                digest=hashlib.sha256(old_identity.encode()).hexdigest()
                candidates.append((parent/'http_cache'/self.provider/(digest+'.json'),old_identity))
            if self.provider=='sofascore':
                digest=hashlib.sha256(json.dumps([endpoint,params],sort_keys=True).encode()).hexdigest()
                for folder in ('browser_cache','extension_cache'):
                    candidates.append((parent/folder/self.provider/(digest+'.json'),None))
        for path,identity in candidates:
            if not path.exists():continue
            saved=json.loads(path.read_text(encoding='utf-8'))
            if identity is not None and saved.get('identity')!=identity:raise ValueError('Retired cache identity mismatch')
            if identity is None and (saved.get('endpoint')!=endpoint or saved.get('params')!=params):raise ValueError('Retired cache endpoint mismatch')
            saved['identity']=self.identity(endpoint,params,kind)
            saved.setdefault('transport','http' if identity is not None else 'extension')
            self._touch(path);return saved
        return None
    def write(self,endpoint,params,kind,value):
        path=self.path(endpoint,params,kind);path.parent.mkdir(parents=True,exist_ok=True)
        value={**value,'identity':self.identity(endpoint,params,kind)}
        temp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
        try:
            with gzip.open(temp,'wt',encoding='utf-8',compresslevel=5) as f:
                json.dump(value,f,ensure_ascii=False,allow_nan=False,separators=(',',':'))
            atomic_replace(temp,path)
        finally:temp.unlink(missing_ok=True)
        self.used[path]=(endpoint,params);self._touch(path)
        cache_root=self.directory.parent
        if time.monotonic()-_LAST_TRIM.get(str(cache_root),float('-inf'))>60:
            self.trim(cache_root);_LAST_TRIM[str(cache_root)]=time.monotonic()
        return path
    def release_match(self,match_id):
        mid=str(match_id)
        for path,(endpoint,params) in list(self.used.items()):
            match=(self.provider=='sofascore' and (endpoint=='event/'+mid or endpoint.startswith('event/'+mid+'/')) or
                   self.provider=='whoscored' and endpoint.lower()=='matches/'+mid+'/live' or
                   self.provider=='fotmob' and endpoint=='matchDetails' and str((params or {}).get('matchId'))==mid)
            if match:
                try:path.unlink(missing_ok=True)
                except OSError:pass
                self.used.pop(path,None)
    @staticmethod
    def trim(directory,max_bytes=MAX_BYTES):
        files=[]
        for p in Path(directory).rglob('*.json.gz'):
            try:s=p.stat();files.append((s.st_mtime,p,s.st_size))
            except FileNotFoundError:pass
        total=sum(x[2] for x in files)
        for _,p,size in sorted(files):
            if total<=max_bytes:break
            try:p.unlink();total-=size
            except OSError:pass
        return total

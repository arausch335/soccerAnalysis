"""Verified immutable files, resumable chunks and a durable local outbox."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import sqlite3
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from uuid import uuid4

from storage.pandasStore import atomic_json

FORMAT='soccer-data-transfer-v1'
CHUNK=1024*1024
MAX_FILE=2*1024**3
MAX_MANIFEST=32*1024**2
FIXTURE_REQUIRED_PATHS = frozenset({'fixtures.csv', *(f'fixture_concordance/{name}' for name in (
    'match_ids.csv', 'team_ids.csv', 'team_aliases.csv', 'issues.csv', 'coverage.csv', 'manifest.json'))})
FIXTURE_DATASET_PATHS = FIXTURE_REQUIRED_PATHS | {'fixture_concordance/attempts.csv','concordance/team_history.csv'}


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(CHUNK),b''):value.update(block)
    return value.hexdigest()


def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def identifier(value):return hashlib.sha256(encode(value)).hexdigest()


def check_hash(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Invalid file digest')
    return value


def safe_path(value,kind):
    if not isinstance(value,str) or '\\' in value or ':' in value:raise ValueError('Invalid transfer path')
    path=PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ('','.','..') for p in value.split('/')):raise ValueError('Transfer path leaves its root')
    if value=='.soccer-scraper.json' and kind=='raw':return value
    if any(p.startswith('.') or p=='_system' for p in path.parts):raise ValueError('Machine state cannot be transferred')
    if path.suffix not in ('.csv','.json'):raise ValueError('Only collection/dataset files may be transferred')
    if kind=='dataset':
        if any(p in ('raw','match_checkpoints','snapshots') for p in path.parts):raise ValueError('Raw data cannot be sent to dataset consumers')
        if len(path.parts)==1 and path.name in ('fixtures.csv','player_ids.csv','team_ids.csv','match_ids.csv'):return value
        if len(path.parts)==2 and path.parts[0]=='concordance':return value
        if len(path.parts)==3 and path.parts[0]=='concordance' and (
                path.parts[1]=='full' and path.name in ('fixtures.csv','entities.csv','teams.csv') or
                path.parts[1]=='scraped' and path.name in ('matches.csv','teams.csv','players.csv')):return value
        if value in FIXTURE_DATASET_PATHS:return value
    if len(path.parts)<3 or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*',path.parts[0]):raise ValueError('Invalid competition path')
    if not re.fullmatch(r'\d{4}-\d{4}',path.parts[1]):raise ValueError('Invalid season path')
    if int(path.parts[1][5:])!=int(path.parts[1][:4])+1:raise ValueError('Invalid season years')
    return value


def validate_manifest(value):
    from .workerIdentity import valid_worker_id
    if not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('kind') not in ('raw','dataset'):raise ValueError('Invalid transfer manifest')
    valid_worker_id(value['worker'])
    files=value.get('files')
    if not isinstance(files,list) or not files or len(files)>100000:raise ValueError('Invalid transfer file list')
    paths=set();sizes={}
    for row in files:
        path=safe_path(row['path'],value['kind']);sha=check_hash(row['sha256']);size=row['size']
        if type(size) is not int or not 0<=size<=MAX_FILE:raise ValueError('Invalid transfer file size')
        if path in paths:raise ValueError('Duplicate transfer path')
        if sha in sizes and sizes[sha]!=size:raise ValueError('Conflicting file sizes')
        paths.add(path);sizes[sha]=size
    if value['kind']=='raw':
        season=value.get('season','')
        if not isinstance(season,str) or season.count('/')!=1:raise ValueError('Invalid raw season')
        safe_path(season+'/config.json','raw')
        if type(value.get('completed')) is not bool:raise ValueError('Missing season completion state')
        if '.soccer-scraper.json' not in paths:raise ValueError('Missing collection marker')
        if any(p!='.soccer-scraper.json' and not p.startswith(season+'/') for p in paths):raise ValueError('Raw manifest contains another season')
    if len(encode(value))>MAX_MANIFEST:raise ValueError('Transfer manifest too large')
    origins=value.get('origins',{})
    if not isinstance(origins,dict):raise ValueError('Invalid origin registry')
    for key,workers in origins.items():
        if not isinstance(key,str) or len(key)>1024 or not isinstance(workers,list):raise ValueError('Invalid origin entries')
        for worker in workers:valid_worker_id(worker)
    return value


def link_or_copy(source,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    try:os.link(source,target)
    except OSError:shutil.copyfile(source,target)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Peer:
    def __init__(self,url,token,*,headers=None):self.url=url.rstrip('/');self.token=token;self.headers=headers or {}

    def request(self,path,value=None,*,method=None,data=None):
        if value is not None:data=encode(value)
        headers={**self.headers,'Authorization':'Bearer '+self.token,'Content-Type':'application/json' if value is not None else 'application/octet-stream'}
        req=Request(self.url+path,data=data,headers=headers,method=method or ('POST' if data is not None else 'GET'))
        with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=15) as response:
            body=response.read(MAX_MANIFEST+1)
        if len(body)>MAX_MANIFEST:raise ValueError('Storage reply too large')
        return json.loads(body)

    def download(self,sha,target):
        sha=check_hash(sha);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
        request=Request(self.url+'/v1/dataset/blobs/'+sha,headers={'Authorization':'Bearer '+self.token})
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=30) as response,target.open('wb') as stream:
            size=0
            while True:
                block=response.read(CHUNK)
                if not block:break
                size+=len(block)
                if size>MAX_FILE:raise ValueError('Dataset file exceeds size limit')
                stream.write(block)
        if digest(target)!=sha:target.unlink(missing_ok=True);raise ValueError('Dataset checksum mismatch')


class Outbox:
    def __init__(self,root,worker,*,directory=None):
        self.root=Path(root);self.worker=worker;self.directory=Path(directory) if directory is not None else self.root/'_system/storage/outbox'
        self.directory.mkdir(parents=True,exist_ok=True);self.database=self.directory/'queue.sqlite'
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,manifest TEXT,state TEXT); '
                             'CREATE TABLE IF NOT EXISTS hashes(path TEXT PRIMARY KEY,signature TEXT,sha TEXT);')

    def connect(self):return closing(sqlite3.connect(self.database,timeout=30))

    def enqueue(self,files,*,kind,season='',completed=False,origins=None):
        rows=[]
        with self.connect() as db:
            for relative,path in sorted(files.items()):
                safe_path(relative,kind);path=Path(path)
                if path.is_symlink():raise ValueError('Transfer source is a symbolic link')
                stat=path.stat();signature=encode([stat.st_size,stat.st_mtime_ns,stat.st_ino]).decode()
                prior=db.execute('SELECT signature,sha FROM hashes WHERE path=?',(str(path),)).fetchone()
                sha=prior[1] if prior and prior[0]==signature else digest(path)
                db.execute('INSERT OR REPLACE INTO hashes VALUES(?,?,?)',(str(path),signature,sha))
                rows.append({'path':relative,'sha256':sha,'size':stat.st_size})
            manifest={'format':FORMAT,'kind':kind,'worker':self.worker,'files':rows}
            if kind=='raw':manifest.update(season=season,completed=completed,origins=origins or {})
            validate_manifest(manifest);mid=identifier(manifest)
            if db.execute('SELECT 1 FROM queue WHERE id=?',(mid,)).fetchone():db.commit();return mid
            from .maintenance import require_space
            require_space(self.directory,sum({r['sha256']:r['size'] for r in rows}.values()))
            stage=self.directory/(mid+'.'+uuid4().hex+'.tmp');stage.mkdir()
            try:
                for row in rows:
                    target=stage/row['sha256']
                    if not target.exists():
                        require_space(self.directory,row['size'])
                        shutil.copyfile(files[row['path']],target)
                        if target.stat().st_size!=row['size'] or digest(target)!=row['sha256']:raise ValueError('Collection changed while queuing; retry after save')
                atomic_json(stage/'manifest.json',manifest)
                destination=self.directory/mid
                if destination.exists():shutil.rmtree(destination)
                os.replace(stage,destination)
                db.execute('INSERT INTO queue VALUES(?,?,?)',(mid,encode(manifest).decode(),'queued'));db.commit()
            finally:
                if stage.exists():shutil.rmtree(stage)
        return mid

    def send_one(self,peer,*,stop=None):
        with self.connect() as db:row=db.execute("SELECT id,manifest FROM queue WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if not row:return False
        mid,body=row;manifest=json.loads(body)
        peer.request('/v1/uploads',manifest)
        for item in manifest['files']:
            if stop is not None and stop.is_set():return False
            sha=item['sha256'];route='/v1/uploads/'+mid+'/blobs/'+sha
            status=peer.request(route)
            if status['complete']:continue
            offset=status['offset'];path=self.directory/mid/sha
            with path.open('rb') as stream:
                stream.seek(offset)
                while offset<item['size']:
                    if stop is not None and stop.is_set():return False
                    block=stream.read(CHUNK)
                    if not block:raise ValueError('Spool file is truncated')
                    value=peer.request(route+'?offset='+str(offset),method='PUT',data=block)
                    offset=value['offset']
        acknowledgement=peer.request('/v1/uploads/'+mid+'/commit',{})
        if acknowledgement.get('id')!=mid or acknowledgement.get('state') not in ('received','imported'):raise ValueError('Storage acknowledgement differs')
        with self.connect() as db:db.execute("UPDATE queue SET state='sent' WHERE id=?",(mid,));db.commit()
        shutil.rmtree(self.directory/mid,ignore_errors=True)
        return True

    def queued(self):
        with self.connect() as db:return db.execute("SELECT count(*) FROM queue WHERE state='queued'").fetchone()[0]

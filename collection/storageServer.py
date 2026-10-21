"""Authenticated MSI archive receiver. Dataset readers never receive raw objects."""
from contextlib import closing
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from urllib.parse import urlsplit, parse_qs

from .storageTransfer import (CHUNK, MAX_MANIFEST, check_hash, digest, encode,
                              identifier, validate_manifest, link_or_copy)
from .workerRuntime import now


class Archive:
    def __init__(self,root,*,reserve=512*1024**2):
        self.directory=Path(root)/'_system/storage/hub';self.objects=self.directory/'objects'
        self.objects.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.reserve=reserve
        self.database=self.directory/'archive.sqlite'
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,owner TEXT,manifest TEXT,state TEXT,received_at TEXT); '
                             'CREATE TABLE IF NOT EXISTS publication(slot INTEGER PRIMARY KEY,manifest TEXT); '
                             'CREATE TABLE IF NOT EXISTS builds(season TEXT PRIMARY KEY,state TEXT);')

    def connect(self):return closing(sqlite3.connect(self.database,timeout=30))

    def register(self,worker,manifest):
        validate_manifest(manifest)
        if manifest['kind']!='raw' or manifest['worker']!=worker:raise PermissionError('Only owned raw collections may be uploaded')
        mid=identifier(manifest)
        with self.lock,self.connect() as db:
            db.execute('INSERT OR IGNORE INTO uploads VALUES(?,?,?,?,?)',(mid,worker,encode(manifest).decode(),'pending',None));db.commit()
        return {'id':mid}

    def upload(self,worker,mid):
        check_hash(mid)
        with self.connect() as db:row=db.execute('SELECT owner,manifest,state FROM uploads WHERE id=?',(mid,)).fetchone()
        if not row or row[0]!=worker:raise PermissionError('Unknown upload')
        return json.loads(row[1]),row[2]

    def descriptor(self,worker,mid,sha):
        check_hash(sha);manifest,state=self.upload(worker,mid)
        item=next((f for f in manifest['files'] if f['sha256']==sha),None)
        if item is None:raise PermissionError('Object is not part of this upload')
        return item,state

    def offset(self,worker,mid,sha):
        item,state=self.descriptor(worker,mid,sha);target=self.objects/sha
        if state in ('received','imported'):return {'complete':True,'offset':item['size']}
        if item['size']==0 and not target.exists():
            target.touch()
            if digest(target)!=sha:target.unlink();raise ValueError('Empty object checksum differs')
        partial=self.objects/(sha+'.part')
        return {'complete':target.exists(),'offset':target.stat().st_size if target.exists() else partial.stat().st_size if partial.exists() else 0}

    def chunk(self,worker,mid,sha,offset,block):
        with self.lock:
            item,state=self.descriptor(worker,mid,sha)
            status=self.offset(worker,mid,sha)
            if status['complete']:return {'offset':item['size']}
            if offset!=status['offset']:raise ValueError('Upload offset differs; query and resume')
            if not block or len(block)>CHUNK or offset+len(block)>item['size']:raise ValueError('Invalid chunk size')
            if shutil.disk_usage(self.objects).free < self.reserve+len(block):raise OSError('MSI archive disk reserve reached')
            partial=self.objects/(sha+'.part')
            with partial.open('ab') as stream:
                stream.write(block);stream.flush()
                import os
                os.fsync(stream.fileno())
            end=offset+len(block)
            if end==item['size']:
                if digest(partial)!=sha:partial.unlink();raise ValueError('Uploaded file checksum differs')
                partial.replace(self.objects/sha)
            return {'offset':end}

    def commit(self,worker,mid):
        with self.lock:
            manifest,state=self.upload(worker,mid)
            if state not in ('received','imported'):
                for item in manifest['files']:
                    if not self.offset(worker,mid,item['sha256'])['complete']:raise ValueError('Upload is incomplete')
                with self.connect() as db:
                    db.execute("UPDATE uploads SET state='received',received_at=? WHERE id=?",(now(),mid));db.commit()
                state='received'
            return {'id':mid,'state':state}

    def completed(self):
        with self.connect() as db:rows=db.execute("SELECT id,manifest FROM uploads WHERE state='received' ORDER BY rowid").fetchall()
        return [(mid,json.loads(body)) for mid,body in rows if json.loads(body).get('completed')]

    def incorporated(self,mid,manifest):
        with self.lock,self.connect() as db:
            # Retire only earlier match packages, never a newer season generation.
            rows=db.execute('SELECT id,manifest FROM uploads WHERE owner=? AND rowid<=(SELECT rowid FROM uploads WHERE id=?)',
                            (manifest['worker'],mid)).fetchall()
            for key,body in rows:
                if json.loads(body).get('season')==manifest['season']:db.execute("UPDATE uploads SET state='imported' WHERE id=?",(key,))
            db.execute("INSERT OR REPLACE INTO builds VALUES(?,'pending')",(manifest['season'],));db.commit()

    def schedule_build(self,season):
        with self.connect() as db:db.execute("INSERT OR REPLACE INTO builds VALUES(?,'pending')",(season,));db.commit()

    def builds(self):
        with self.connect() as db:return [r[0] for r in db.execute("SELECT season FROM builds WHERE state='pending'")]

    def publish(self,manifest,files):
        validate_manifest(manifest)
        if manifest['kind']!='dataset':raise ValueError('Expected dataset publication')
        with self.lock:
            for item in manifest['files']:
                target=self.objects/item['sha256']
                if not target.exists():
                    # Copy: later in-place edits to a dataset must not alter immutable objects.
                    temporary=self.objects/(item['sha256']+'.publish')
                    shutil.copyfile(files[item['path']],temporary)
                    if digest(temporary)!=item['sha256']:temporary.unlink();raise ValueError('Dataset changed while publishing')
                    temporary.replace(target)
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO publication VALUES(1,?)',(encode(manifest).decode(),));db.commit()

    def publication(self):
        with self.connect() as db:row=db.execute('SELECT manifest FROM publication WHERE slot=1').fetchone()
        return json.loads(row[0]) if row else None

    def collected(self):
        """Remove transfer duplicates only after incorporation; keep ledger receipts."""
        with self.lock,self.connect() as db:
            manifests=[json.loads(r[0]) for r in db.execute("SELECT manifest FROM uploads WHERE state!='imported'")]
            publication=self.publication()
            if publication:manifests.append(publication)
            keep={f['sha256'] for m in manifests for f in m['files']}
            for path in self.objects.iterdir():
                key=path.name.removesuffix('.part').removesuffix('.publish')
                if len(key)==64 and key not in keep:path.unlink()

    def status(self):
        with self.connect() as db:
            counts=dict(db.execute('SELECT state,count(*) FROM uploads GROUP BY state'))
            last=db.execute('SELECT max(received_at) FROM uploads').fetchone()[0]
        publication=self.publication()
        return {'received':counts.get('received',0),'incorporated':counts.get('imported',0),'last_received':last,
                'dataset_id':identifier(publication) if publication else None}


class StorageServer:
    def __init__(self,root,options,*,reserve=512*1024**2):
        self.archive=Archive(root,reserve=reserve);self.options=options
        archive=self.archive
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def setup(self):super().setup();self.connection.settimeout(30)
            def reply(self,value,status=200):
                body=encode(value);self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def authenticate(self):
                if self.headers.get('Origin'):raise PermissionError('Browser requests are not accepted')
                token=self.headers.get('Authorization','')
                for worker,credential in options['tokens'].items():
                    if hmac.compare_digest(token,'Bearer '+credential['token']):return worker,credential['role']
                raise PermissionError('Storage authentication failed')
            def body(self,limit):
                length=int(self.headers.get('Content-Length','-1'))
                if not 0<=length<=limit:raise ValueError('Request body exceeds limit')
                body=self.rfile.read(length)
                if len(body)!=length:raise ValueError('Truncated request')
                return body
            def dispatch(self):
                try:
                    worker,role=self.authenticate();url=urlsplit(self.path);parts=url.path.strip('/').split('/')
                    if self.command=='GET' and parts==['v1','status']:return self.reply(archive.status())
                    if role=='consumer' and self.command=='GET':
                        publication=archive.publication()
                        if parts==['v1','dataset']:return self.reply({'manifest':publication})
                        if len(parts)==4 and parts[:3]==['v1','dataset','blobs']:
                            sha=check_hash(parts[3])
                            if not publication or not any(f['sha256']==sha for f in publication['files']):raise PermissionError('Not a published dataset object')
                            # Hold lock until streaming ends so publication GC cannot remove this object on Windows.
                            with archive.lock,(archive.objects/sha).open('rb') as stream:
                                self.send_response(200);self.send_header('Content-Length',str((archive.objects/sha).stat().st_size));self.end_headers()
                                shutil.copyfileobj(stream,self.wfile,CHUNK)
                            return
                    if role!='producer':raise PermissionError('Role does not allow this operation')
                    if self.command=='POST' and parts==['v1','uploads']:
                        return self.reply(archive.register(worker,json.loads(self.body(MAX_MANIFEST))))
                    if len(parts)==5 and parts[:2]==['v1','uploads'] and parts[3]=='blobs':
                        if self.command=='GET':
                            with archive.lock:return self.reply(archive.offset(worker,parts[2],parts[4]))
                        if self.command=='PUT':return self.reply(archive.chunk(worker,parts[2],parts[4],int(parse_qs(url.query)['offset'][0]),self.body(CHUNK)))
                    if self.command=='POST' and len(parts)==4 and parts[:2]==['v1','uploads'] and parts[3]=='commit':
                        self.body(MAX_MANIFEST);return self.reply(archive.commit(worker,parts[2]))
                    self.reply({'error':'Unknown storage endpoint'},404)
                except PermissionError:self.reply({'error':'Storage operation not authorized'},403)
                except (ValueError,KeyError,TypeError,UnicodeError,json.JSONDecodeError):self.reply({'error':'Invalid storage request'},400)
                except OSError:self.reply({'error':'Storage unavailable; queued files remain on producer'},503)
            do_GET=dispatch;do_POST=dispatch;do_PUT=dispatch
        url=urlsplit(options['url'])
        self.server=ThreadingHTTPServer((url.hostname,url.port or 8767),Handler)
        self.server.daemon_threads=True;self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.url=f'http://{url.hostname}:{self.server.server_port}'
    def start(self):self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

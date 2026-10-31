"""Authenticated MSI archive receiver. Dataset readers never receive raw objects."""
from contextlib import closing
import hmac
from http.server import ThreadingHTTPServer
from storage.httpServer import DisconnectSafeHandler, CLIENT_DISCONNECTED
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
    def __init__(self,root,*,reserve=1024**3,directory=None):
        self.directory=Path(directory) if directory is not None else Path(root)/'_system/storage/hub';self.objects=self.directory/'objects'
        self.objects.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.reserve=reserve;self.publishing=False;self.readers=0
        self.database=self.directory/'archive.sqlite'
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,owner TEXT,manifest TEXT,state TEXT,received_at TEXT); '
                             'CREATE TABLE IF NOT EXISTS upload_failures(id INTEGER PRIMARY KEY AUTOINCREMENT,upload_id TEXT,at TEXT,error TEXT,retried_at TEXT); '
                             'CREATE TABLE IF NOT EXISTS publication(slot INTEGER PRIMARY KEY,manifest TEXT); '
                             'CREATE TABLE IF NOT EXISTS builds(season TEXT PRIMARY KEY,state TEXT,revision INTEGER NOT NULL DEFAULT 0); '
                             'CREATE TABLE IF NOT EXISTS build_failures(id INTEGER PRIMARY KEY AUTOINCREMENT,season TEXT,revision INTEGER,at TEXT,error TEXT);')
            db.execute('BEGIN IMMEDIATE')
            if 'revision' not in {row[1] for row in db.execute('PRAGMA table_info(builds)')}:
                db.execute('ALTER TABLE builds ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            db.commit()

    def connect(self):return closing(sqlite3.connect(self.database,timeout=30))

    def register(self,worker,manifest):
        validate_manifest(manifest)
        if manifest['kind']!='raw' or manifest['worker']!=worker:raise PermissionError('Only owned raw collections may be uploaded')
        mid=identifier(manifest)
        from .removedCollections import removed_seasons
        state='removed' if manifest['season'] in removed_seasons() else 'pending'
        with self.lock,self.connect() as db:
            db.execute('INSERT OR IGNORE INTO uploads VALUES(?,?,?,?,?)',(mid,worker,encode(manifest).decode(),state,None));db.commit()
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
        if state in ('received','imported','quarantined','removed'):return {'complete':True,'offset':item['size']}
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
            if shutil.disk_usage(self.objects).free < self.reserve+item['size']-offset:raise OSError('MSI archive disk reserve reached')
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
            if state not in ('received','imported','quarantined','removed'):
                for item in manifest['files']:
                    if not self.offset(worker,mid,item['sha256'])['complete']:raise ValueError('Upload is incomplete')
                with self.connect() as db:
                    db.execute("UPDATE uploads SET state='received',received_at=? WHERE id=?",(now(),mid));db.commit()
                state='received'
            # Older producers understand received/imported acknowledgements. A
            # quarantined upload is still durably received; replay must not
            # requeue it or force producers to resend preserved raw bytes.
            return {'id':mid,'state':'received' if state in ('quarantined','removed') else state,
                    **({'integration_state':state} if state in ('quarantined','removed') else {})}

    def completed(self):
        with self.connect() as db:rows=db.execute("SELECT id,manifest FROM uploads WHERE state='received' ORDER BY rowid").fetchall()
        # Received means the upload is complete, even while its season is unfinished.
        return [(mid,json.loads(body)) for mid,body in rows]

    def quarantine(self,mid,error):
        """Set one invalid upload aside, retaining its manifest and all objects."""
        check_hash(mid)
        with self.lock,self.connect() as db:
            changed=db.execute("UPDATE uploads SET state='quarantined' WHERE id=? AND state='received'",(mid,)).rowcount
            if changed:
                db.execute('INSERT INTO upload_failures(upload_id,at,error) VALUES(?,?,?)',
                           (mid,now(),(type(error).__name__+': '+str(error))[:4096]))
            db.commit()
        return bool(changed)

    def quarantined(self):
        with self.connect() as db:
            rows=db.execute("SELECT u.id,u.owner,u.manifest,f.at,f.error FROM uploads u "
                            "LEFT JOIN upload_failures f ON f.id=(SELECT max(id) FROM upload_failures WHERE upload_id=u.id) "
                            "WHERE u.state='quarantined' ORDER BY u.rowid").fetchall()
        return [{'id':mid,'worker':owner,'season':json.loads(body).get('season',''),
                 'quarantined_at':at,'error':error} for mid,owner,body,at,error in rows]

    def retry(self,mid):
        """Explicitly retry a reviewed upload without deleting its failure history."""
        check_hash(mid)
        with self.lock,self.connect() as db:
            changed=db.execute("UPDATE uploads SET state='received' WHERE id=? AND state='quarantined'",(mid,)).rowcount
            if not changed:raise ValueError('Upload is not set aside for review')
            db.execute('UPDATE upload_failures SET retried_at=? WHERE upload_id=? AND retried_at IS NULL',(now(),mid))
            db.commit()

    def incorporated(self,mid,manifest):
        with self.lock,self.connect() as db:
            db.execute("UPDATE uploads SET state='imported' WHERE id=?",(mid,))
            if manifest.get('completed'):
                # A full-season snapshot supersedes earlier packages. A single
                # match must never acknowledge another match that was deferred.
                rows=db.execute("SELECT id,manifest FROM uploads WHERE owner=? AND state NOT IN ('quarantined','removed') AND rowid<=(SELECT rowid FROM uploads WHERE id=?)",
                                (manifest['worker'],mid)).fetchall()
                for key,body in rows:
                    if json.loads(body).get('season')==manifest['season']:db.execute("UPDATE uploads SET state='imported' WHERE id=?",(key,))
                db.execute("INSERT INTO builds(season,state,revision) VALUES(?,'pending',1) ON CONFLICT(season) DO UPDATE SET state='pending',revision=revision+1",(manifest['season'],))
            db.commit()

    def schedule_build(self,season):
        from .removedCollections import removed_seasons
        if season in removed_seasons():
            self.remove_season(season);return
        with self.connect() as db:db.execute("INSERT INTO builds(season,state,revision) VALUES(?,'pending',1) ON CONFLICT(season) DO UPDATE SET state='pending',revision=revision+1",(season,));db.commit()

    def remove_season(self,season):
        """Retain receipts while removing this season from active build/import work."""
        with self.lock,self.connect() as db:
            db.execute("UPDATE builds SET state='removed' WHERE season=?",(season,))
            for mid,body in db.execute("SELECT id,manifest FROM uploads WHERE state!='removed'").fetchall():
                if json.loads(body).get('season')==season:db.execute("UPDATE uploads SET state='removed' WHERE id=?",(mid,))
            db.commit()

    def block_build(self,season,revision,error):
        with self.connect() as db:
            db.execute("UPDATE builds SET state='blocked' WHERE season=? AND revision=?",(season,revision))
            db.execute('INSERT INTO build_failures(season,revision,at,error) VALUES(?,?,?,?)',
                       (season,revision,now(),(type(error).__name__+': '+str(error))[:4096]));db.commit()

    def diagnostics(self):
        with self.connect() as db:
            builds=[dict(season=s,state=state,revision=revision,error=error,failed_at=at) for s,state,revision,error,at in db.execute(
                "SELECT b.season,b.state,b.revision,f.error,f.at FROM builds b LEFT JOIN build_failures f ON f.id="
                "(SELECT max(id) FROM build_failures WHERE season=b.season) ORDER BY b.season")]
        quarantined=self.quarantined();details={}
        for row in quarantined:
            import re
            found=re.search(r'([a-f0-9]{64})\.json',row.get('error') or '')
            if not found:continue
            key=(row['season'],found[1])
            if key not in details and len(details)<20:
                with self.connect() as db:record=db.execute('SELECT manifest FROM uploads WHERE id=?',(row['id'],)).fetchone()
                try:details[key]=self.quarantine_identity(json.loads(record[0]),found[1]) if record else {}
                except (OSError,ValueError,KeyError,TypeError):details[key]={'available':False}
            if key in details:row['identity_comparison']=details[key]
        from .removedCollections import removed_seasons
        return dict(format='soccer-storage-diagnostics-v1',checked_at=now(),status=self.status(),
                    builds=builds,quarantined_uploads=quarantined,removed_raw_seasons=removed_seasons())

    def quarantine_identity(self,manifest,filename):
        """Return identity fields only; event/stat payloads remain on MSI."""
        import csv,io
        entries=manifest['files']
        raw=next((item for item in entries if item['path'].endswith('/raw/whoscored/'+filename+'.json')),None)
        if raw is None:return {'available':False}
        def read(item):
            path=self.objects/check_hash(item['sha256'])
            if path.stat().st_size>32*1024**2:raise ValueError('Identity diagnostic object too large')
            return path.read_text(encoding='utf-8-sig')
        payload=json.loads(read(raw))['payload'];centre=payload['matchCentreData'];mid=str(payload['match_id'])
        result={'available':True,'whoscored_id':mid,'date':str(centre.get('startDate',''))[:10],
            'teams':{side:{'id':centre.get(side,{}).get('teamId'),'name':centre.get(side,{}).get('name','')}
                     for side in ('home','away')},'checkpoints':[]}
        for item in entries:
            if not item['path'].endswith('/matches.csv') or '/match_checkpoints/' not in item['path']:continue
            for row in csv.DictReader(io.StringIO(read(item))):
                metadata=json.loads(row['metadata'])
                if str(metadata.get('provider_match_ids',{}).get('whoscored'))!=mid:continue
                teamfile=next((entry for entry in entries if entry['path']==item['path'].removesuffix('matches.csv')+'teams.csv'),None)
                names={r['id']:json.loads(r['name']) for r in csv.DictReader(io.StringIO(read(teamfile)))} if teamfile else {}
                result['checkpoints'].append({'match_id':row['id'],'provider':metadata.get('id_namespace','sofascore'),
                    'date':json.loads(row['kickoff'])[:10],
                    'teams':{side:{'id':row[side+'_team_id'],'name':names.get(row[side+'_team_id'],'')}
                             for side in ('home','away')}})
        return result

    def builds(self):
        with self.connect() as db:return [r[0] for r in db.execute("SELECT season FROM builds WHERE state='pending'")]

    def build_jobs(self):
        with self.connect() as db:return list(db.execute("SELECT season,revision FROM builds WHERE state='pending'"))

    def published(self,season,revision):
        with self.connect() as db:
            db.execute("UPDATE builds SET state='published' WHERE season=? AND revision=?",(season,revision));db.commit()

    def publish(self,manifest,files,*,check=lambda:None):
        validate_manifest(manifest)
        if manifest['kind']!='dataset':raise ValueError('Expected dataset publication')
        with self.lock:self.publishing=True
        try:
            # Object copying/checksumming must not hold the HTTP receiver lock:
            # Headless can continue uploading while a large dataset is published.
            from .maintenance import require_space
            for item in manifest['files']:
                check();target=self.objects/item['sha256']
                if not target.exists():
                    require_space(self.objects,item['size'],reserve=self.reserve)
                    temporary=self.objects/(item['sha256']+'.publish')
                    try:
                        shutil.copyfile(files[item['path']],temporary)
                        if digest(temporary)!=item['sha256']:raise ValueError('Dataset changed while publishing')
                        with self.lock:temporary.replace(target)
                    finally:temporary.unlink(missing_ok=True)
            check()
            with self.lock,self.connect() as db:
                db.execute('INSERT OR REPLACE INTO publication VALUES(1,?)',(encode(manifest).decode(),));db.commit()
        finally:
            with self.lock:self.publishing=False

    def publication(self):
        with self.connect() as db:row=db.execute('SELECT manifest FROM publication WHERE slot=1').fetchone()
        return json.loads(row[0]) if row else None

    def collected(self):
        """Remove transfer duplicates only after incorporation; keep ledger receipts."""
        with self.lock,self.connect() as db:
            if self.publishing or self.readers:return
            manifests=[json.loads(r[0]) for r in db.execute("SELECT manifest FROM uploads WHERE state NOT IN ('imported','removed')")]
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
        with self.connect() as db:builds=dict(db.execute('SELECT state,count(*) FROM builds GROUP BY state'))
        return {'pending_builds':builds.get('pending',0),'blocked_builds':builds.get('blocked',0),'removed_builds':builds.get('removed',0),'received':counts.get('received',0),'incorporated':counts.get('imported',0),
                'quarantined':counts.get('quarantined',0),'last_received':last,
                'dataset_id':identifier(publication) if publication else None}


class StorageServer:
    def __init__(self,root,options,*,reserve=1024**3):
        self.archive=Archive(root,reserve=reserve);self.options=options
        archive=self.archive
        class Handler(DisconnectSafeHandler):
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
                        if parts==['v1','diagnostics']:return self.reply(archive.diagnostics())
                        if parts==['v1','dataset']:return self.reply({'manifest':publication})
                        if len(parts)==4 and parts[:3]==['v1','dataset','blobs']:
                            sha=check_hash(parts[3])
                            if not publication or not any(f['sha256']==sha for f in publication['files']):raise PermissionError('Not a published dataset object')
                            # Pin streaming readers without stopping producer uploads.
                            with archive.lock:
                                stream=(archive.objects/sha).open('rb');archive.readers+=1
                            try:
                                with stream:
                                    self.send_response(200);self.send_header('Content-Length',str((archive.objects/sha).stat().st_size));self.end_headers()
                                    shutil.copyfileobj(stream,self.wfile,CHUNK)
                            finally:
                                with archive.lock:archive.readers-=1
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
                except CLIENT_DISCONNECTED:raise
                except OSError:self.reply({'error':'Storage unavailable; queued files remain on producer'},503)
            do_GET=dispatch;do_POST=dispatch;do_PUT=dispatch
        url=urlsplit(options['url'])
        self.server=ThreadingHTTPServer((url.hostname,url.port or 8767),Handler)
        self.server.daemon_threads=True;self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.url=f'http://{url.hostname}:{self.server.server_port}'
    def start(self):self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

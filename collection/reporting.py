"""Durable worker reporting over a private HTTP connection; Git remains code-only.

Only this module owns the reporting databases. Other computers use the HTTP API,
never a shared SQLite file. No third-party service or extra dependency is needed.
"""
from storage.layout import system_path,organize_data_root
from storage.sqlitePaths import readonly_uri
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from storage.httpServer import DisconnectSafeHandler, CLIENT_DISCONNECTED
import argparse
import gzip
import io
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import uuid4

from storage.pandasStore import atomic_json
from filelock import FileLock,Timeout
from .workerIdentity import valid_worker_id

MAX_BODY = 64 * 1024 * 1024
INVENTORY_FORMAT = 'soccer-analysis-progress-v1'
RUNTIME_FORMAT = 'soccer-worker-runtime-v1'


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def private_ip(value):
    address = ipaddress.IPv4Address(value)
    return any(address in ipaddress.ip_network(net) for net in
               ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10'))


def validate_url(value):
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Use a reporting URL such as http://100.x.y.z:8766 without credentials or a path')
    if url.scheme == 'http':
        try:
            allowed = private_ip(url.hostname)
        except ValueError:
            allowed = url.hostname == 'localhost'
        if not allowed:
            raise ValueError('Plain HTTP reporting requires a private IPv4 address; use the Main Tailscale IP')
    # Force validation of a malformed port now, before a worker starts.
    port=url.port
    if url.scheme=='http' and port is None:return f'http://{url.hostname}:8766'
    return value.rstrip('/')


def load_local(root):
    path = system_path(Path(root),'reporting.json')
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(saved, dict) or saved.get('mode') not in ('server', 'client'):
        raise ValueError('Invalid local reporting configuration')
    if saved['mode'] == 'server':
        if not private_ip(saved['bind']):
            raise ValueError('Bind reporting to the Main Tailscale or private IPv4 address')
        if type(saved['port']) is not int or not 1024 <= saved['port'] <= 65535:
            raise ValueError('Invalid reporting port')
        tokens = saved['tokens']
        if not isinstance(tokens, dict) or not tokens:
            raise ValueError('Configure at least one worker token')
        for worker, token in tokens.items():
            valid_worker_id(worker)
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError('Reporting tokens must be at least 32 characters')
        if len(set(tokens.values())) != len(tokens):
            raise ValueError('Each worker needs a different reporting token')
    else:
        saved['url'] = validate_url(saved['url'])
        valid_worker_id(saved['worker_id'])
        if not isinstance(saved.get('token'), str) or len(saved['token']) < 32:
            raise ValueError('Reporting token must be at least 32 characters')
    return saved


def inventory_paths(ensemble, *, local_worker=None):
    """Direct reports supersede historical Git snapshots, never an active local holder."""
    ensemble = Path(ensemble)
    paths = {p.stem: p for p in (ensemble / 'workers').glob('*.json')}
    for path in (ensemble / 'reporting' / 'workers').glob('*.json'):
        if path.stem != local_worker or path.stem not in paths:
            paths[path.stem] = path
    return [paths[key] for key in sorted(paths)]


class ReportingStore:
    """Main-owned state, imported historical inventories and bounded remote log tails."""
    def __init__(self, root, ensemble):
        self.path = system_path(Path(root),'worker_runtime') / 'reporting.sqlite'
        self.ensemble = Path(ensemble)
        self.root=Path(root)
        from providers.identityReview import ReviewStore
        self.identities=ReviewStore(root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workers (
                    worker TEXT PRIMARY KEY, instance TEXT, stream TEXT, sequence INTEGER DEFAULT 0,
                    inventory TEXT, version TEXT, runtime TEXT, last_seen TEXT);
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, at TEXT, level TEXT, message TEXT);
                CREATE INDEX IF NOT EXISTS logs_worker ON logs(worker, id);
            ''')
        # Existing durable reports always win over an older Git inventory.
        from .ensembleProgress import read_progress
        for path in inventory_paths(ensemble):
            saved = read_progress(path)
            if saved.get('worker_id') != path.stem:
                raise ValueError('Imported reporting inventory identity differs')
            with self.connect() as db:
                db.execute('INSERT OR IGNORE INTO workers(worker,instance,inventory,version) VALUES(?,?,?,?)',
                           (path.stem, saved.get('instance_id'), encode(saved), uuid4().hex))
        self.materialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:yield db
        finally:
            db.close()

    def materialize(self, selected=None, *, inventories=True):
        with self.lock, self.connect() as db:
            for row in db.execute('SELECT * FROM workers'):
                worker = valid_worker_id(row['worker'])
                if selected is not None and worker not in selected:
                    continue
                if inventories and row['inventory']:
                    atomic_json(self.ensemble / 'reporting' / 'workers' / (worker + '.json'), json.loads(row['inventory']))
                if row['runtime']:
                    runtime = json.loads(row['runtime'])
                    runtime['received_at'] = row['last_seen']
                    atomic_json(self.ensemble / 'reporting' / 'runtime' / (worker + '.json'), runtime)

    def accept(self, worker, events):
        valid_worker_id(worker)
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise ValueError('Invalid report batch')
        changed_inventory = False
        suggestions_by_id = {}
        validated_suggestions = {}
        with self.lock, self.connect() as db:
            row = db.execute('SELECT * FROM workers WHERE worker=?', (worker,)).fetchone()
            inventory = None  # Decode a potentially large inventory only for a delta.
            inventory_json = row['inventory'] if row else None
            version = row['version'] if row else None
            runtime_json = row['runtime'] if row else None
            sequence_seen = row['sequence'] if row else 0
            instance_seen = row['instance'] if row else None
            stream_seen = row['stream'] if row else None
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError('Invalid report')
                instance, stream, sequence = event['instance_id'], event['stream'], event['sequence']
                if not all(isinstance(v, str) and 1 <= len(v) <= 128 for v in (instance, stream)) or type(sequence) is not int or sequence < 1:
                    raise ValueError('Invalid reporting identity or sequence')
                if instance_seen and instance_seen != instance:
                    raise ValueError('Worker ID belongs to a different data folder')
                if stream_seen and stream_seen != stream:
                    raise ValueError('Reporting queue identity changed; restore the original worker queue')
                # Suggestions are replayed after commit to repair an interrupted export,
                # but identical proposals within a catch-up batch need processing once.
                from providers.identityReview import validate,unique_contexts
                suggestions = event.get('identity_suggestions', [])
                if not isinstance(suggestions, list) or len(suggestions) > 500:
                    raise ValueError('Invalid identity suggestions')
                for suggestion in suggestions:
                    # Observation timestamps do not change identity evidence.
                    normalized = ({k:v for k,v in suggestion.items() if k not in ('first_seen','last_seen')}
                                  if isinstance(suggestion, dict) else suggestion)
                    encoded = encode(normalized)
                    if encoded not in validated_suggestions:
                        validated_suggestions[encoded] = validate(normalized)
                    proposal = dict(validated_suggestions[encoded])
                    prior = suggestions_by_id.get(proposal['id'])
                    if prior:
                        contexts = prior.get('contexts', []) + proposal.get('contexts', [])
                        proposal['contexts'] = unique_contexts(contexts)
                        if prior.get('investigation', {}).get('checked_at', '') > proposal.get('investigation', {}).get('checked_at', ''):
                            proposal['investigation'] = prior['investigation']
                    suggestions_by_id[proposal['id']] = proposal
                if sequence <= sequence_seen:
                    continue  # Acknowledgement was lost; replay has no log side effects.
                update = event.get('inventory')
                if update is not None:
                    if not isinstance(update, dict) or update.get('worker_id') != worker or update.get('instance_id') != instance or update.get('format') != INVENTORY_FORMAT or not isinstance(update.get('records'), dict):
                        raise ValueError('Inventory identity differs from the authenticated worker')
                    for key, record in update['records'].items():
                        if not isinstance(record, dict) or not all(isinstance(record.get(f), str) for f in ('namespace', 'competition', 'season', 'match_id')):
                            raise ValueError('Invalid inventory record')
                        for field in ('capabilities', 'origins', 'holders'):
                            if not isinstance(record.get(field, []), list) or any(not isinstance(v, str) for v in record.get(field, [])):
                                raise ValueError('Invalid inventory evidence')
                    if inventory is None and inventory_json is not None:
                        inventory = json.loads(inventory_json)
                    if inventory is None or event.get('reset'):
                        inventory = {**update, 'records': {}}
                    if 'schedules' in update:
                        if not isinstance(update['schedules'],dict):raise ValueError('Invalid inventory schedules')
                        inventory['schedules']=update['schedules']
                    inventory['records'].update(update['records'])
                    for key in event.get('removed', []):
                        inventory['records'].pop(key, None)
                    inventory['updated_at'] = update.get('updated_at', now())
                    version = uuid4().hex
                    changed_inventory = True
                runtime = event.get('runtime')
                if runtime is not None and (not isinstance(runtime, dict) or runtime.get('worker_id') != worker or runtime.get('format') != RUNTIME_FORMAT):
                    raise ValueError('Runtime identity differs from the authenticated worker')
                logs = event.get('logs', [])
                if not isinstance(logs, list) or len(logs) > 100:
                    raise ValueError('Invalid logs')
                for log in logs:
                    if not isinstance(log, dict) or not isinstance(log.get('message'), str) or len(log['message']) > 4000 or log.get('level') not in ('INFO', 'WARNING', 'ERROR') or not isinstance(log.get('at'), str):
                        raise ValueError('Invalid log entry')
                    db.execute('INSERT INTO logs(worker,at,level,message) VALUES(?,?,?,?)',
                               (worker, log['at'], log['level'], log['message']))
                fixture=event.get('fixture_run')
                if fixture is not None:
                    from fixtures.progress import validate
                    validate(fixture,worker)
                if runtime is not None or fixture is not None:
                    previous=json.loads(runtime_json) if runtime_json else {}
                    combined=dict(runtime) if runtime is not None else previous
                    # Fixture and match-stat processes share an identity, but
                    # neither may replace the other's progress snapshots.
                    combined.pop('fixture_run',None)
                    if previous.get('fixture_run'):combined['fixture_run']=previous['fixture_run']
                    if fixture is not None:
                        combined['fixture_run']={**fixture,'received_at':now()}
                    if not combined.get('format'):
                        combined.update(format=RUNTIME_FORMAT,worker_id=worker,state='fixture_only',updated_at=now())
                    runtime_json=encode(combined)
                instance_seen, stream_seen, sequence_seen = instance, stream, sequence
            # One write per batch, rather than decoding/re-encoding the complete
            # inventory for every log or heartbeat in a 50-event catch-up batch.
            if changed_inventory:inventory_json = encode(inventory)
            db.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?,?,?,?,?,?)',
                       (worker, instance_seen, stream_seen, sequence_seen, inventory_json,
                        version, runtime_json, now()))
            db.execute('DELETE FROM logs WHERE worker=? AND id NOT IN (SELECT id FROM logs WHERE worker=? ORDER BY id DESC LIMIT 10000)', (worker, worker))
        # Idempotent replay repairs suggestions as well as runtime views after an interrupted export.
        proposals = list(suggestions_by_id.values())
        for index in range(0, len(proposals), 500):
            self.identities.ingest(proposals[index:index + 500], worker)
        # Retry also repairs materialized views if a previous response/export was interrupted.
        self.materialize({worker}, inventories=changed_inventory or any(e.get('inventory') is not None for e in events))
        return events[-1]['sequence']

    def inventories(self, versions, exclude=None, reep_version=''):
        from providers.identityReview import decisions
        result = {}; current = {}
        with self.lock, self.connect() as db:
            for row in db.execute('SELECT worker,inventory,version FROM workers WHERE inventory IS NOT NULL'):
                if row['worker'] == exclude:
                    continue
                current[row['worker']] = row['version']
                if versions.get(row['worker']) != row['version']:
                    result[row['worker']] = json.loads(row['inventory'])
        from providers.reep import snapshot
        reep=snapshot(self.root)
        return {'inventories': result, 'versions': current,'identity_decisions':decisions(self.root),
                'reep_index':reep if reep and reep['version']!=reep_version else None}


def read_logs(root, worker='', level='', search='', limit=200, after=0, *, focus=''):
    path = system_path(Path(root),'worker_runtime') / 'reporting.sqlite'
    if not path.exists():
        return {'rows': [], 'latest_id': 0}
    # Read-only: dashboard never creates or modifies the reporting database.
    with closing(sqlite3.connect(readonly_uri(path.resolve()), uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        clauses = ['id > ?']; args = [after]
        if worker:
            clauses.append('worker=?'); args.append(worker)
        if level:
            clauses.append('level=?'); args.append(level)
        milestones="(message LIKE '[Season started] %' OR message LIKE '[Season completed] %' OR message LIKE '[Season paused] %' OR message LIKE '=== START:%' OR message LIKE '=== FINISHED:%' OR message LIKE '=== PAUSED:%')"
        important={
            'important':"(level IN ('WARNING','ERROR') OR message LIKE '[Storage] %' OR message LIKE '[Fixtures] %' OR "+milestones+")",
            'fixtures':"message LIKE '[Fixtures] %'",
            'errors':"level='ERROR'",
            'warnings':"level IN ('WARNING','ERROR')",
            'seasons':"message LIKE '[Season completed] %'",
            'milestones':milestones,
        }
        if focus in important:clauses.append(important[focus])
        if search:
            clauses.append('instr(lower(message),lower(?))>0'); args.append(search[:200])
        rows = [dict(r) for r in db.execute('SELECT * FROM logs WHERE ' + ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT ?', (*args, min(500, max(1, limit))))]
        latest = db.execute('SELECT COALESCE(MAX(id),0) FROM logs').fetchone()[0]
    return {'rows': list(reversed(rows)), 'latest_id': latest}


class ReportingServer:
    def __init__(self, root, ensemble, options, *, storage_plan=None,workers=None,command_plan=None,dataset_root=None):
        self.store = ReportingStore(root, ensemble)
        if dataset_root is None:
            import config
            dataset_root = config.DATASET_ROOT
        from .compiledDelivery import CompiledReceiver
        self.compiled = CompiledReceiver(root, dataset_root)
        compiled = self.compiled
        tokens = options['tokens']
        store = self.store
        provision=None;control=None
        if command_plan is not None:
            from .jobControl import JobControl
            control=JobControl(root,command_plan)
        if storage_plan and storage_plan.get('enabled'):
            from .storageConfig import Provisioner
            provision=Provisioner(root,storage_plan,workers or list(tokens))
        class Handler(DisconnectSafeHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                data = encode(value).encode()
                compressed='gzip' in self.headers.get('Accept-Encoding','') and len(data)>1024
                if compressed:data=gzip.compress(data,compresslevel=1)
                self.send_response(code)
                if compressed:self.send_header('Content-Encoding','gzip')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError,TimeoutError):pass

            def authorized(self):
                header = self.headers.get('Authorization', '')
                supplied = header[7:] if header.startswith('Bearer ') else ''
                return next((worker for worker, token in tokens.items() if hmac.compare_digest(token.encode(), supplied.encode())), None)

            def do_GET(self):
                self.connection.settimeout(10)
                worker = self.authorized()
                if not worker:
                    self.reply(401, {'error': 'Worker authentication required'}); return
                if self.path.startswith('/v1/compiled/'):
                    compiled.dispatch(self, worker, store); return
                if self.path=='/v1/identity-registry':
                    try:
                        from concordance.sync import snapshot
                        value=snapshot(root)
                        self.reply(200,{'snapshot':value if value and value['version']!=self.headers.get('X-Registry-Version','') else None})
                    except (OSError,ValueError):self.reply(503,{'error':'Identity registry temporarily unavailable'})
                    return
                if self.path=='/v1/health':
                    with store.lock,store.connect() as db:
                        row=db.execute('SELECT instance,stream FROM workers WHERE worker=?',(worker,)).fetchone()
                    instance=self.headers.get('X-Worker-Instance')
                    stream=self.headers.get('X-Reporting-Stream')
                    self.reply(200,{'worker_id':worker,'identity_matches':not row or not row['instance'] or not instance or row['instance']==instance,
                                    'queue_matches':not row or not row['stream'] or not stream or row['stream']==stream})
                    return
                if self.path != '/v1/inventories':
                    self.reply(404, {'error': 'Unknown endpoint'}); return
                try:
                    versions = json.loads(self.headers.get('X-Inventory-Versions', '{}'))
                    if not isinstance(versions, dict):
                        raise ValueError('Invalid inventory versions')
                    value=store.inventories(versions, exclude=worker,
                                           reep_version=self.headers.get('X-Reep-Version',''))
                    if provision:value['storage_config']=provision.for_worker(worker)
                    if control:value['instructions']=control.instructions(worker,store)
                    self.reply(200,value)
                except (ValueError, TypeError):
                    self.reply(400, {'error': 'Invalid request'})

            def do_POST(self):
                self.connection.settimeout(10)
                worker = self.authorized()
                if not worker:
                    self.reply(401, {'error': 'Worker authentication required'}); return
                if self.path.startswith('/v1/compiled/'):
                    compiled.dispatch(self, worker, store); return
                if self.path=='/v1/assignments' and control:
                    try:
                        length=int(self.headers.get('Content-Length','0'))
                        if not 0<length<=16384:raise ValueError('Invalid command length')
                        value=json.loads(self.rfile.read(length))
                        if not isinstance(value,dict):raise ValueError('Invalid command')
                        with store.lock,store.connect() as db:
                            row=db.execute('SELECT instance FROM workers WHERE worker=?',(worker,)).fetchone()
                        if row and row['instance'] and row['instance']!=value.get('instance'):
                            self.reply(409,{'error':'Worker identity differs'});return
                        result=control.command(worker,value)
                        if result.get('state')=='granted':result['instructions']=control.instructions(worker,store)
                        self.reply(200,result)
                    except (ValueError,KeyError,TypeError):self.reply(400,{'error':'Invalid assignment request'})
                    except CLIENT_DISCONNECTED:raise
                    except (OSError,sqlite3.Error):self.reply(503,{'error':'Assignment coordinator unavailable; no new work authorized'})
                    return
                if self.path != '/v1/report':
                    self.reply(404, {'error': 'Unknown endpoint'}); return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                        self.reply(413, {'error': 'Invalid report size'}); return
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('Incomplete report')
                    value = json.loads(raw)
                    self.reply(200, {'ack': store.accept(worker, value['events'])})
                except (ValueError, KeyError, TypeError):
                    self.reply(409, {'error': 'Report rejected: check worker identity, original queue and report format locally'})
                except CLIENT_DISCONNECTED:raise
                except (OSError, sqlite3.Error):
                    self.reply(503, {'error': 'Report storage unavailable; retry later'})
            def do_PUT(self):
                self.connection.settimeout(30)
                worker = self.authorized()
                if not worker:
                    self.reply(401, {'error': 'Worker authentication required'}); return
                if self.path.startswith('/v1/compiled/'):
                    compiled.dispatch(self, worker, store); return
                self.reply(404, {'error': 'Unknown endpoint'})
        self.server = ThreadingHTTPServer((options['bind'], options['port']), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='worker-report-receiver', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward a worker token to a redirected endpoint.


class Reporter:
    """Persist reports before returning to the scraper; all network I/O is background."""
    def __init__(self, root, ensemble, worker, options, heartbeat=30):
        self.root = Path(root); self.ensemble = Path(ensemble); self.worker = worker
        self.options = options; self.url = validate_url(options['url']) if options.get('url') else None; self.heartbeat = heartbeat
        self.identity = json.loads((system_path(self.root,'identity.json')).read_text(encoding='utf-8'))
        if options['worker_id'] != worker or self.identity['worker_id'] != worker:
            raise ValueError('Reporting token is configured for a different worker')
        self.path = system_path(self.root,'worker_runtime') / 'reporting-outbox.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock(); self.wake = threading.Event(); self.stop = threading.Event(); self.thread = None; self.peer_thread = None; self.compiled_thread = None
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.status = {'state': 'connecting' if self.url else 'unconfigured', 'message': 'Connecting to Main; reports are saved locally.' if self.url else 'Reporting setup pending. Progress and logs are saved in the local queue.'}
        self.versions = {}
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS known(key TEXT PRIMARY KEY, hash TEXT);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);''')
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('stream', uuid4().hex))
            self.stream = db.execute("SELECT value FROM meta WHERE key='stream'").fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:yield db
        finally:
            db.close()

    def _insert(self, db, kind, value):
        db.execute('INSERT INTO queue(kind,payload) VALUES(?,?)', (kind, encode(value)))

    def enqueue(self, *, inventory=True, reset=False):
        with self.lock, self.connect() as db:
            payload = {}
            runtime = system_path(self.root,'worker_runtime') / (self.worker + '.json')
            if runtime.exists():
                payload['runtime'] = json.loads(runtime.read_text(encoding='utf-8'))
            path = self.ensemble / 'workers' / (self.worker + '.json')
            if inventory and path.exists():
                from .ensembleProgress import read_progress
                saved = read_progress(path)
                if saved.get('worker_id') != self.worker or saved.get('instance_id') != self.identity['instance_id']:
                    raise ValueError('Local reporting inventory identity differs')
                known = dict(db.execute('SELECT key,hash FROM known'))
                hashes = {key: hashlib.sha256(encode(row).encode()).hexdigest() for key, row in saved['records'].items()}
                changed = {key: row for key, row in saved['records'].items() if reset or hashes[key] != known.get(key)}
                removed = list(set(known) - set(hashes))
                schedule_hash=hashlib.sha256(encode(saved.get('schedules',{})).encode()).hexdigest()
                old_schedules=db.execute("SELECT value FROM meta WHERE key='inventory_schedules'").fetchone()
                schedules_changed=old_schedules!=(schedule_hash,)
                if reset or changed or removed or schedules_changed:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('inventory_schedules',?)",(schedule_hash,))
                    payload.update(inventory={**saved, 'records': changed}, reset=reset, removed=removed)
                    db.execute('DELETE FROM known')
                    db.executemany('INSERT INTO known VALUES(?,?)', hashes.items())
            from providers.identityReview import ReviewStore
            proposals=ReviewStore(self.root).proposals()
            proposals=sorted(proposals,key=lambda p:p['id'])
            # Receiving the same shared proposal refreshes its local observation
            # timestamps. Those timestamps must not generate another full upload.
            stable = [{k:v for k,v in p.items() if k not in ('first_seen','last_seen')} for p in proposals]
            digest=hashlib.sha256(encode(stable).encode()).hexdigest()
            prior=db.execute("SELECT value FROM meta WHERE key='identity_suggestions_hash'").fetchone()
            if reset or prior is None or prior[0]!=digest:
                # Separate durable batches are never coalesced with heartbeats.
                for index in range(0,len(proposals),500):self._insert(db,'identities',{'identity_suggestions':proposals[index:index+500]})
                db.execute("INSERT OR REPLACE INTO meta VALUES('identity_suggestions_hash',?)",(digest,))
            # Coalesce unsent heartbeats, while retaining every saved-match report and log.
            kind = 'snapshot' if inventory else 'heartbeat'
            if kind == 'heartbeat':
                db.execute("DELETE FROM queue WHERE kind='heartbeat'")
            self._insert(db, kind, payload)
        self.wake.set()

    def enqueue_fixture(self,value):
        from fixtures.progress import validate
        validate(value,self.worker)
        with self.lock,self.connect() as db:
            db.execute("DELETE FROM queue WHERE kind='fixture'")
            self._insert(db,'fixture',{'fixture_run':value})
        self.wake.set()

    def log(self, message, level='INFO'):
        message = redact(message, self.options['token'], str(self.root), str(self.ensemble))
        if not message.strip():
            return
        with self.lock, self.connect() as db:
            self._insert(db, 'logs', {'logs': [{'at': now(), 'level': level, 'message': message[:4000]}]})
        self.wake.set()

    def request(self, path, payload=None, headers=None):
        request = Request(self.url + path, data=encode(payload).encode() if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + self.options['token'], 'Content-Type': 'application/json', 'Accept-Encoding':'gzip', **(headers or {})})
        with self.opener.open(request, timeout=15 if path=='/v1/inventories' else 30) as response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError('Reporting response exceeds size limit')
            if response.headers.get('Content-Encoding')=='gzip':
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:data=compressed.read(MAX_BODY+1)
                if len(data)>MAX_BODY:raise ValueError('Reporting response exceeds size limit')
            return json.loads(data)

    def flush(self):
        # The stats worker and fixture CLI can share one durable queue.
        try:
            with FileLock(str(self.path)+'.send.lock',timeout=0):return self._flush()
        except Timeout:return False

    def _flush(self):
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT id,payload FROM queue ORDER BY id LIMIT 50').fetchall()
        if not rows:
            return False
        size=0; batch=[]
        for row in rows:
            if batch and size+len(row[1].encode())>8*1024*1024:break
            batch.append(row);size+=len(row[1].encode())
        rows=batch
        events = [{**json.loads(payload), 'instance_id': self.identity['instance_id'], 'stream': self.stream, 'sequence': seq} for seq, payload in rows]
        result = self.request('/v1/report', {'events': events})
        if result.get('ack') != rows[-1][0]:
            raise ValueError('Reporting acknowledgement differs')
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM queue WHERE id<=?', (result['ack'],))
        if self.status.get('state')!='connected':print('[Reporting] Connected to Main. Progress and logs are being delivered.',flush=True)
        self.status = {'state': 'connected', 'message': 'Connected to Main. Saved matches and logs report in the background.', 'last_sent_at': now()}
        return True

    def pull(self):
        from providers.reep import version,install
        value = self.request('/v1/inventories', headers={'X-Inventory-Versions': encode(self.versions),
                                                      'X-Reep-Version':version(self.root)})
        if value.get('instructions'):
            from .workInstructions import install as install_instructions
            install_instructions(self.root,value['instructions'],worker=self.worker)
        if value.get('storage_config'):
            from .storageConfig import install as install_storage
            install_storage(self.root,value['storage_config'])
        if value.get('reep_index'):
            install(self.root,value['reep_index'])
        for worker, saved in value['inventories'].items():
            valid_worker_id(worker)
            if worker == self.worker:
                continue
            if saved.get('worker_id') != worker or saved.get('format') != INVENTORY_FORMAT or not isinstance(saved.get('records'), dict):
                raise ValueError('Received inventory identity differs')
            atomic_json(self.ensemble / 'reporting' / 'workers' / (worker + '.json'), saved)
        if 'identity_decisions' in value:
            from providers.identityReview import ReviewStore,decisions
            if decisions(self.root)!=value['identity_decisions']:
                ReviewStore(self.root).replace_decisions(value['identity_decisions'])
                print('[Identity review] Shared decisions updated; incomplete matches will be revalidated in a saved-checkpoint pass.',flush=True)
        self.versions = value['versions']
        self.pull_registry()

    def pull_registry(self):
        from concordance.sync import installed_version,install
        local=load_local(self.root)
        if local and local.get('mode')=='server':return
        try:value=self.request('/v1/identity-registry',headers={'X-Registry-Version':installed_version(self.root)})
        except HTTPError as exc:
            if exc.code==404:return  # Backward-compatible with an older Main.
            raise
        if value.get('snapshot'):install(self.root,value['snapshot'])

    def start(self,*,snapshot=None):
        enqueue=snapshot or self.enqueue
        enqueue(reset=True)
        if not self.url:return self
        def send():
            next_heartbeat = 0; retry_at = 0
            while not self.stop.is_set():
                self.wake.clear()
                current = time.monotonic()
                if current < retry_at:
                    self.wake.wait(min(5,retry_at-current))
                    continue
                try:
                    if current >= next_heartbeat:
                        enqueue(inventory=False)
                        next_heartbeat = current + self.heartbeat
                    # A bounded batch yields to shutdown and heartbeat checks during catch-up.
                    if current >= retry_at and self.flush():
                        self.wake.set()
                    retry_at = 0
                except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                    message=connection_error(exc)
                    if self.status.get('message')!=message:print('[Reporting] '+message,flush=True)
                    self.status = {'state': 'offline', 'message': message}
                    retry_at = time.monotonic() + 10
                    next_heartbeat = max(next_heartbeat, retry_at)
                delay = max(0.1, retry_at - time.monotonic()) if retry_at else max(0.1, min(5, next_heartbeat - time.monotonic()))
                self.wake.wait(delay)
        def receive_inventories():
            warned=False
            while not self.stop.is_set():
                try:
                    self.pull()
                    warned=False
                except (OSError,ValueError,KeyError,sqlite3.Error):
                    if not warned:print('[Reporting] Other-worker inventory download delayed. Progress and logs use a separate connection.',flush=True)
                    warned=True
                self.stop.wait(self.heartbeat)
        def send_compiled():
            from .compiledDelivery import flush
            while not self.stop.is_set():
                try:flush(self.root, stop=self.stop)
                except (OSError, ValueError, KeyError, sqlite3.Error):pass
                self.stop.wait(30)
        self.compiled_thread = threading.Thread(target=send_compiled, name='compiled-data-to-main', daemon=True)
        self.compiled_thread.start()
        self.thread = threading.Thread(target=send, name='worker-reporter', daemon=True)
        if snapshot is None:self.peer_thread=threading.Thread(target=receive_inventories,name='worker-inventory-receiver',daemon=True)
        self.thread.start()
        if self.peer_thread:self.peer_thread.start()
        return self

    def close(self,*,grace=0):
        if grace:self.wake.set();self.stop.wait(grace)
        # No synchronous network drain: an offline Main must not hold up stopping.
        self.stop.set(); self.wake.set()
        if self.thread:self.thread.join(timeout=6)
        if self.peer_thread:self.peer_thread.join(timeout=16)
        if self.compiled_thread:self.compiled_thread.join(timeout=2)


def connection_error(exc):
    if isinstance(exc,HTTPError):
        if exc.code==401:return 'Main rejected the worker token (HTTP 401). Reconfigure this worker with its matching token. Reports remain queued.'
        if exc.code==409:return 'Main rejected the worker identity or reporting queue (HTTP 409). Run python -m collection.reporting status. Reports remain queued.'
        return f'Main returned HTTP {exc.code}. Collection continues; reports and logs remain queued.'
    from urllib.error import URLError
    if isinstance(exc, TimeoutError) or (isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)):
        return 'Main did not acknowledge the report before the request timed out. Reports remain queued and retry automatically.'
    return 'Cannot deliver reports to Main. Check its configured address, Tailscale connection and receiver. Collection continues; reports remain queued.'


def diagnose(root):
    """Read-only local/network checks; never display a token or submit a report."""
    root=Path(root);options=load_local(root)
    if not options:
        print('Reporting is not configured. Run the client/server setup command first.');return 2
    identity_path=system_path(root,'identity.json')
    identity=json.loads(identity_path.read_text(encoding='utf-8')) if identity_path.exists() else {}
    worker=identity.get('worker_id',options.get('worker_id','main'))
    print('Worker:',worker)
    if options['mode']=='server':
        url=f"http://{options['bind']}:{options['port']}";token=options['tokens'].get(worker,'')
    else:url=options['url'];token=options['token']
    print('Main address:',url)
    state_path=system_path(root,'worker_runtime')/(worker+'.json')
    if state_path.exists():
        state=json.loads(state_path.read_text(encoding='utf-8'))
        print('Local worker state:',state.get('state'))
        print('Last reporting status:',state.get('reporting',{}).get('message','Not reported yet'))
    outbox=system_path(root,'worker_runtime')/'reporting-outbox.sqlite';stream=None
    if outbox.exists():
        with closing(sqlite3.connect(readonly_uri(outbox.resolve()),uri=True)) as db:
            print('Queued reports/log batches:',db.execute('SELECT COUNT(*) FROM queue').fetchone()[0])
            row=db.execute("SELECT value FROM meta WHERE key='stream'").fetchone();stream=row[0] if row else None
    headers={'Authorization':'Bearer '+token,'X-Worker-Instance':identity.get('instance_id','')}
    if stream:headers['X-Reporting-Stream']=stream
    try:
        request=Request(url+'/v1/health',headers=headers)
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=8) as response:value=json.load(response)
        if value['worker_id']!=worker:
            print('TOKEN MISMATCH: token belongs to a different worker. Use this worker’s matching token from Main.');return 2
        if not value['identity_matches']:
            print('IDENTITY CONFLICT: Main has a different original data folder registered for this worker. Preserve both identities and queues; do not delete them.');return 2
        if not value['queue_matches']:
            print('QUEUE CONFLICT: Main has a different original reporting queue registered. Restore the original queue; do not delete it.');return 2
        print('Connection OK: Main is reachable, token accepted, worker identity and queue match.')
        print('This health check does not confirm that queued reports have been acknowledged.');return 0
    except HTTPError as exc:
        if exc.code==404:
            print('Main is reachable and accepted the token. Restart Main with the latest code for full diagnostics.');return 0
        print(connection_error(exc));return 2
    except (OSError,ValueError,KeyError) as exc:
        print(connection_error(exc));return 2


def redact(message, *secrets_to_hide):
    text = str(message)
    for secret in sorted((s for s in secrets_to_hide if s), key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    text = re.sub(r'https?://[^\s<>\"\']+', '[url]', text, flags=re.I)
    text = re.sub(r'''(?i)(["']?(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*).*''', r'\1[redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', text)
    text = re.sub(r'''(?ix)(["']?(?:access[_-]?token|refresh[_-]?token|token|password|secret|api[_-]?key)["']?\s*[:=]\s*)(?:["'][^"']*["']|[^\s,;}]+)''', r'\1[redacted]', text)
    return text


def console_level(line, default='INFO'):
    # A summary with zero failures is informational, not an error.
    text=re.sub(r'\b0\s+(?:failed|errors?)\b','',line,flags=re.I)
    if re.search(r'\b(error|exception|traceback|failed)\b',text,re.I):return 'ERROR'
    if ': partial (' in text:return 'WARNING'
    if re.search(r'\b(warn|warning|paused|retry)\b',text,re.I):return 'WARNING'
    return default


@contextmanager
def capture_logs(reporter):
    class Tee:
        def __init__(self, stream, level):
            self.stream = stream; self.level = level; self.pending = ''; self.lock = threading.RLock()
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, text):
            result = self.stream.write(text)
            with self.lock:
                self.pending += text.replace('\r', '\n')
                while '\n' in self.pending or len(self.pending) > 4000:
                    line, separator, rest = self.pending.partition('\n')
                    if not separator:
                        line, rest = self.pending[:4000], self.pending[4000:]
                    self.pending = rest
                    try:
                        level = console_level(line,self.level)
                        reporter.log(line, level)
                    except (OSError, ValueError, sqlite3.Error):
                        pass  # Console remains available if local reporting storage fills.
            return result
        def flush(self):
            self.stream.flush()
    stdout, stderr = sys.stdout, sys.stderr
    out, err = Tee(stdout, 'INFO'), Tee(stderr, 'ERROR')
    sys.stdout, sys.stderr = out, err
    from .workerLogs import observe_logs
    try:
        with observe_logs(reporter.log):yield
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        for tee in (out, err):
            if tee.pending:
                try:reporter.log(tee.pending, tee.level)
                except (OSError,ValueError,sqlite3.Error):pass


def main(argv=None):
    import config
    from config.workerPlan import load_plan
    from getpass import getpass
    parser = argparse.ArgumentParser(description='Configure private worker reporting; settings stay in ignored local data')
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status',help='Check reporting connection and queue without printing tokens')
    server = commands.add_parser('server', help='Configure Main’s receiver (does not start scraping)')
    server.add_argument('--bind', required=True, help='Main Tailscale IPv4 address')
    server.add_argument('--port', type=int, default=8766)
    client = commands.add_parser('client', help='Configure this worker; asks privately for its token')
    client.add_argument('--worker-id', required=True)
    client.add_argument('--url', required=True)
    args = parser.parse_args(argv)
    if args.command=='status':return diagnose(args.data_root)
    organize_data_root(args.data_root)
    path = system_path(args.data_root,'reporting.json')
    if args.command == 'server':
        if not private_ip(args.bind) or not 1024 <= args.port <= 65535:
            parser.error('Use a private/Tailscale IPv4 address and port 1024–65535')
        previous = load_local(args.data_root) if path.exists() else None
        if previous and previous['mode'] != 'server':
            parser.error('This data folder is configured as a reporting client; review its local reporting.json first')
        plan = load_plan()
        tokens = dict(previous['tokens']) if previous else {}
        for worker in plan.workers:
            tokens.setdefault(worker, secrets.token_urlsafe(32))
        saved = {'mode': 'server', 'bind': args.bind, 'port': args.port, 'tokens': tokens}
    else:
        plan = load_plan(); plan.for_worker(args.worker_id)
        saved = {'mode': 'client', 'worker_id': args.worker_id, 'url': validate_url(args.url), 'token': getpass('Worker token from Main (hidden): ').strip()}
        if len(saved['token']) < 32:
            parser.error('Copy the complete worker token from Main')
    marker=args.data_root/'.soccer-scraper.json'
    if not marker.exists():
        if args.data_root.exists() and any(args.data_root.iterdir()):parser.error('Use the original marked scraper data folder, or an empty new folder')
        atomic_json(marker,{'format':1})
    atomic_json(path, saved, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f'Local reporting configuration saved in {path}. Keep this file private; do not commit it.')
    if args.command == 'server':
        for worker, token in tokens.items():
            if worker==plan.coordinator:continue
            print(f'\n{worker}: python -m collection.reporting client --worker-id {worker} --url http://{args.bind}:{args.port}')
            print(f'Worker token: {token}')
    print('\nRestart python main.py on this computer to use the configuration.')


if __name__ == '__main__':
    raise SystemExit(main())

"""Authenticated loopback command bridge for the user's chosen Chrome tab."""
from contextlib import contextmanager
from contextvars import ContextVar
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
import time

from .httpClient import ProviderError, ProviderRequestError

_ACTIVE = ContextVar('sofascore_extension_bridge', default=None)
MAX_BODY = 16 * 1024 * 1024


class ExtensionBridge:
    def __init__(self, *, report=print, connect_timeout=120, diagnostics_dir=None):
        self.report = report
        self.connect_timeout = connect_timeout
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.serial = threading.Lock()
        self.connected = threading.Event()
        self.server = None
        self.thread = None
        self.client_id = None
        self.pending = None
        self.closed = False
        self.phase = 'waiting'
        self.user_paused = False
        self.unavailable = False
        self.waiting_seconds = 0.0
        from .extensionDiagnostics import ExtensionDiagnostics
        self.diagnostics = ExtensionDiagnostics(diagnostics_dir, report) if diagnostics_dir is not None else None

    def record(self, event, **fields):
        if self.diagnostics is not None:self.diagnostics.record(event, **fields)

    def start(self):
        if self.closed:
            raise ProviderError('Extension connection is closed')
        if self.server is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log connection credentials or football response bodies.

            def setup(self):
                super().setup()
                self.connection.settimeout(25)

            def reply(self, status, value):
                body = json.dumps(value, ensure_ascii=True).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                origin = self.headers.get('Origin', '')
                if re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self, *, client=True):
                origin = self.headers.get('Origin', '')
                if (self.headers.get('Host') != bridge.address or
                        origin and not re.fullmatch(r'chrome-extension://[a-p]{32}', origin)):
                    self.reply(403, {'error': 'Origin/host not allowed'})
                    return False
                if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + bridge.token).encode('utf-8')):
                    self.reply(401, {'error': 'Connection code required'})
                    return False
                if client and (not bridge.client_id or self.headers.get('X-Collector-Client') != bridge.client_id):
                    self.reply(409, {'error': 'Connect this extension first'})
                    return False
                return True

            def body(self):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError('Invalid message size')
                    value = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(value, dict):
                        raise ValueError('Expected an object')
                    return value
                except (ValueError, UnicodeError, OSError):
                    self.reply(400, {'error': 'Invalid JSON message'})
                    return None

            def do_OPTIONS(self):
                origin = self.headers.get('Origin', '')
                if self.headers.get('Host') != bridge.address or not re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
                    self.reply(403, {'error': 'Origin not allowed'})
                    return
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Methods', 'GET, POST')
                self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Collector-Client')
                self.end_headers()

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path == '/v1/command':
                    with bridge.condition:
                        bridge.condition.wait_for(lambda: bridge.pending is not None or bridge.closed, timeout=15)
                        item = bridge.pending
                        command = ({k: item[k] for k in ('id', 'action', 'args')}
                                   if item and item['result'] is None else None)
                    self.reply(200, {'command': command, 'phase': bridge.phase})
                else:
                    self.reply(404, {'error': 'Unknown route'})

            def do_POST(self):
                if not self.authorized(client=self.path != '/v1/connect'):
                    return
                value = self.body()
                if value is None:
                    return
                with bridge.condition:
                    if self.path == '/v1/connect':
                        cid = value.get('client_id', '')
                        if not isinstance(cid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', cid):
                            self.reply(400, {'error': 'Invalid client ID'})
                            return
                        if bridge.client_id and bridge.client_id != cid:
                            self.reply(409, {'error': 'Another tab is connected; disconnect it first'})
                            return
                        bridge.client_id = cid
                        bridge.connected.set()
                        bridge.user_paused = False
                        bridge.unavailable = False
                        bridge.phase = 'connected'
                        bridge.record('connected')
                        self.reply(200, {'version': 1, 'message': 'Connected to Python'})
                    elif self.path == '/v1/result':
                        if not bridge.pending or value.get('id') != bridge.pending['id']:
                            self.reply(409, {'error': 'Command expired; resume the Python collection'})
                            return
                        result = value.get('result')
                        if not isinstance(result, dict):
                            self.reply(400, {'error': 'Expected result object'})
                            return
                        bridge.pending['result'] = result
                        bridge.condition.notify_all()
                        self.reply(200, {'saved': True})
                    elif self.path == '/v1/diagnostic':
                        if (not bridge.pending or bridge.pending['action'] != 'verify' or
                                bridge.pending['result'] is not None or value.get('id') != bridge.pending['id']):
                            self.reply(409, {'error': 'No matching verification command'})
                            return
                        result = value.get('result')
                        if not isinstance(result, dict):
                            self.reply(400, {'error': 'Expected diagnostic object'})
                            return
                        bridge.record('verification_progress', action='verify', result=result)
                        self.reply(200, {'saved': True})
                    elif self.path in ('/v1/pause', '/v1/resume'):
                        bridge.user_paused = self.path == '/v1/pause'
                        bridge.record('paused' if bridge.user_paused else 'resumed')
                        bridge.condition.notify_all()
                        self.reply(200, {'paused': bridge.user_paused})
                    elif self.path == '/v1/disconnect':
                        bridge.connected.clear()
                        bridge.unavailable = True
                        bridge.client_id = None
                        bridge.phase = 'disconnected'
                        bridge.record('disconnected')
                        if bridge.pending:
                            bridge.pending['result'] = {'ok': False, 'error': 'Extension disconnected; reconnect and resume collection'}
                        bridge.condition.notify_all()
                        self.reply(200, {'disconnected': True})
                    else:
                        self.reply(404, {'error': 'Unknown route'})

        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except OSError as exc:
            raise ProviderError('Could not start the local extension connection; allow local Python connections and try again') from exc
        self.server.daemon_threads = True
        self.address = f'127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if self.diagnostics is not None:
            self.record('started')
            self.report(f'Extension diagnostic log: {self.diagnostics.path}', flush=True)
        self.report('Open Sofascore in your normal Chrome, open Soccer Collector, and paste this connection code:', flush=True)
        self.report(f'http://{self.address}/#{self.token}', flush=True)

    def request(self, action, args=None, *, timeout=45):
        started = time.monotonic()
        self.record('command_started', action=action, args=args or {})
        try:
            result = self._request(action, args, timeout=timeout)
        except (ProviderError, ValueError) as exc:
            self.record('command_failed', action=action, args=args or {},
                        result={'error': str(exc), 'status': getattr(exc, 'status_code', None)},
                        elapsed=time.monotonic()-started)
            raise
        self.record('command_finished', action=action, args=args or {}, result=result,
                    elapsed=time.monotonic()-started)
        return result

    def _request(self, action, args=None, *, timeout=45):
        if action not in ('navigate', 'page', 'verify'):
            raise ValueError('Unknown extension action')
        self.start()
        if self.unavailable:
            raise ProviderRequestError('Extension needs attention; reconnect and resume collection', attempts=0)
        connection_started = time.monotonic()
        connected = self.connected.wait(self.connect_timeout)
        self.waiting_seconds += time.monotonic() - connection_started
        if not connected:
            self.unavailable = True
            raise ProviderRequestError('Chrome extension not connected; click Connect and run again', attempts=0)
        with self.serial:
            item = {'id': secrets.token_hex(16), 'action': action, 'args': args or {}, 'result': None}
            with self.condition:
                if self.closed:
                    raise ProviderError('Extension connection is closed')
                if not self.client_id:
                    raise ProviderRequestError('Extension disconnected; reconnect and resume', attempts=0)
                self.pending = item
                self.phase = 'verification' if action == 'verify' else 'collecting'
                self.condition.notify_all()
                try:
                    remaining = float(timeout)
                    last = time.monotonic()
                    was_paused = self.user_paused or action == 'verify'
                    while item['result'] is None and not self.closed:
                        self.condition.wait(timeout=None if was_paused else max(0, remaining))
                        current = time.monotonic()
                        if was_paused:
                            self.waiting_seconds += current - last
                        else:
                            remaining -= current - last
                        last = current
                        was_paused = self.user_paused or action == 'verify'
                        if remaining <= 0 and not was_paused:
                            break
                    if item['result'] is None:
                        self.unavailable = True
                        raise ProviderRequestError('Extension step timed out; keep saved data and resume when ready',
                                                   status_code=403 if action == 'verify' else None, attempts=1)
                    result = item['result']
                finally:
                    self.pending = None
                    self.condition.notify_all()
            if not result.get('ok', False):
                self.record('page_failure', action=action, args=args or {}, result=result)
                self.unavailable = True
                raise ProviderRequestError(str(result.get('error', 'Extension step failed')),
                                           status_code=result.get('status'), attempts=1)
            return result

    def close(self):
        self.record('closed')
        with self.condition:
            self.closed = True
            self.phase = 'closed'
            self.connected.set()
            self.condition.notify_all()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)


@contextmanager
def extension_scope():
    """Reuse one connection across a main.py batch; launch it only if needed."""
    existing = _ACTIVE.get()
    if existing is not None:
        yield existing
        return
    resources = []
    token = _ACTIVE.set(resources)
    try:
        yield resources
    finally:
        _ACTIVE.reset(token)
        for bridge in resources:
            bridge.close()


def acquire_bridge(*, report=print, connect_timeout=120, diagnostics_dir=None):
    resources = _ACTIVE.get()
    if resources is None:
        return ExtensionBridge(report=report, connect_timeout=connect_timeout, diagnostics_dir=diagnostics_dir), True
    if not resources:
        resources.append(ExtensionBridge(report=report, connect_timeout=connect_timeout, diagnostics_dir=diagnostics_dir))
    return resources[0], False

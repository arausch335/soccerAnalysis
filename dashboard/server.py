"""Loopback dashboard with a protected, explicit identity-review action."""
from http.server import ThreadingHTTPServer
from storage.httpServer import DisconnectSafeHandler, CLIENT_DISCONNECTED
import json
import sqlite3
import secrets
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from .model import DashboardModel


class DashboardServer:
    def __init__(self,root,ensemble,plan,worker,port):
        model=DashboardModel(root,ensemble,plan,worker)
        assets=Path(__file__).parent
        review_token=secrets.token_urlsafe(32)
        class Handler(DisconnectSafeHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                host=self.headers.get('Host','')
                if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
                    self.send_error(403);return
                url=urlsplit(self.path)
                try:
                    if url.path=='/api/status':data=json.dumps(model.status()).encode();kind='application/json'
                    elif url.path=='/api/identity-review':data=json.dumps({**model.reviews(),'review_token':review_token}).encode();kind='application/json'
                    elif url.path=='/api/collection-review':data=json.dumps({**model.collection_reviews(),'review_token':review_token}).encode();kind='application/json'
                    elif url.path=='/api/logs':data=json.dumps(model.logs(parse_qs(url.query))).encode();kind='application/json'
                    elif url.path=='/api/matches':data=json.dumps(model.matches(parse_qs(url.query))).encode();kind='application/json'
                    elif url.path in ('/','/dashboard.css','/dashboard.js'):
                        name='index.html' if url.path=='/' else url.path[1:]
                        data=(assets/name).read_bytes();kind={'html':'text/html','css':'text/css','js':'text/javascript'}[name.rsplit('.',1)[-1]]
                    else:self.send_error(404);return
                    self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
                    self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except CLIENT_DISCONNECTED:
                    pass  # The browser closed this request; no response can be sent.
                except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
                    self.send_error(503,'Dashboard configuration or inventory needs review')
            def do_POST(self):
                host=self.headers.get('Host','')
                allowed=(f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
                origin=self.headers.get('Origin')
                if (host not in allowed or origin not in ('http://'+h for h in allowed)
                        or not secrets.compare_digest(self.headers.get('X-Review-Token',''),review_token)):
                    self.send_error(403);return
                if self.path not in ('/api/identity-review','/api/collection-review'):self.send_error(404);return
                try:
                    self.connection.settimeout(5)
                    length=int(self.headers.get('Content-Length','0'))
                    if (not 0<length<=4096 or self.headers.get('Transfer-Encoding')
                            or self.headers.get('Content-Type')!='application/json'):
                        self.send_error(400);return
                    value=json.loads(self.rfile.read(length))
                    if self.path=='/api/identity-review':model.decide_identity(value)
                    else:model.decide_collection(value)
                    data=json.dumps({'saved':True}).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)))
                    self.end_headers();self.wfile.write(data)
                except (ValueError,TypeError,KeyError) as exc:
                    data=json.dumps({'error':str(exc)}).encode()
                    self.send_response(409);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except CLIENT_DISCONNECTED:raise
                except (OSError,sqlite3.Error):self.send_error(503,'Identity review storage unavailable')
        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,name='collection-dashboard',daemon=True)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def start(self):self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)


def start_server(root,ensemble,plan,worker,port=8765):return DashboardServer(root,ensemble,plan,worker,port).start()

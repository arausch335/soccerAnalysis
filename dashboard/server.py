"""Loopback-only HTTP server with a read-only, allowlisted API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from .model import DashboardModel


class DashboardServer:
    def __init__(self,root,ensemble,plan,worker,port):
        model=DashboardModel(root,ensemble,plan,worker)
        assets=Path(__file__).parent
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                host=self.headers.get('Host','')
                if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
                    self.send_error(403);return
                url=urlsplit(self.path)
                try:
                    if url.path=='/api/status':data=json.dumps(model.status()).encode();kind='application/json'
                    elif url.path=='/api/matches':data=json.dumps(model.matches(parse_qs(url.query))).encode();kind='application/json'
                    elif url.path in ('/','/dashboard.css','/dashboard.js'):
                        name='index.html' if url.path=='/' else url.path[1:]
                        data=(assets/name).read_bytes();kind={'html':'text/html','css':'text/css','js':'text/javascript'}[name.rsplit('.',1)[-1]]
                    else:self.send_error(404);return
                    self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
                    self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except (OSError,ValueError,TypeError,KeyError):
                    self.send_error(503,'Dashboard configuration or inventory needs review')
        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,name='collection-dashboard',daemon=True)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def start(self):self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)


def start_server(root,ensemble,plan,worker,port=8765):return DashboardServer(root,ensemble,plan,worker,port).start()

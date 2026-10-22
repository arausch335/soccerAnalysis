"""Normal peer disconnects end one HTTP request without a server traceback."""
from http.server import BaseHTTPRequestHandler

CLIENT_DISCONNECTED=(BrokenPipeError,ConnectionResetError,ConnectionAbortedError)


class DisconnectSafeHandler(BaseHTTPRequestHandler):
    def handle(self):
        try:super().handle()
        except CLIENT_DISCONNECTED:
            self.close_connection=True

    def finish(self):
        # BaseRequestHandler always finishes, including after a failed response.
        try:super().finish()
        except CLIENT_DISCONNECTED:pass

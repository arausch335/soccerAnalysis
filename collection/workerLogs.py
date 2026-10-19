"""Optional process-wide operational log sink, including browser bridge threads."""
from contextlib import contextmanager
import threading

_lock = threading.RLock()
_sink = None


def operational_log(message, level='INFO'):
    with _lock:
        sink = _sink
    if sink is not None:
        try:
            sink(message, level)
        except Exception:
            pass  # Diagnostic forwarding must never interrupt browser recovery.


@contextmanager
def observe_logs(sink):
    global _sink
    with _lock:
        previous, _sink = _sink, sink
    try:
        yield
    finally:
        with _lock:
            _sink = previous

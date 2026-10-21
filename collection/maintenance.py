"""Small, cooperative background jobs; foreground season work has priority."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import shutil
import json
import os
import time
from filelock import FileLock

RESERVE = 1024**3  # Keep one GiB for new scraper checkpoints and system work.


class DeferredWork(Exception):
    """Keep the durable task queued and try again after foreground work."""


class LowSpace(DeferredWork):
    pass


def available(path, *, reserve=RESERVE):
    path=Path(path)
    while not path.exists():path=path.parent
    return max(0, shutil.disk_usage(path).free-reserve)


def require_space(path, size, *, reserve=RESERVE):
    free=available(path,reserve=reserve)
    if size>free:
        raise LowSpace(f'Background work waiting for disk space: needs {size:,} bytes; {free:,} available after reserve')


def lock_paths(root, season):
    directory=Path(root)/'_system/locks/seasons';directory.mkdir(parents=True,exist_ok=True)
    key=hashlib.sha256(season.encode()).hexdigest()[:16]
    return directory/(key+'.lock'), directory/(key+'.waiting')


def waiting_for_foreground(path):
    if not path.exists():return False
    import psutil
    try:
        saved=json.loads(path.read_text())
        if psutil.Process(saved['pid']).create_time()==saved['created']:return True
    except (psutil.NoSuchProcess,ProcessLookupError):pass
    except (OSError,ValueError,KeyError):
        try:
            if time.time()-path.stat().st_mtime<60:return True
        except FileNotFoundError:return False
    except psutil.AccessDenied:return True
    path.unlink(missing_ok=True)
    return False


@contextmanager
def foreground(root, season):
    """Request priority before waiting for a background reader to yield."""
    path,waiting=lock_paths(root,season)
    import psutil
    from storage.pandasStore import atomic_json
    atomic_json(waiting,{'pid':os.getpid(),'created':psutil.Process().create_time()})
    try:
        with FileLock(str(path)):
            waiting.unlink(missing_ok=True)
            yield
    finally:waiting.unlink(missing_ok=True)


class Checkpoint:
    def __init__(self, stop=None, waiting=None):self.stop=stop;self.waiting=waiting
    def is_set(self):
        return bool((self.stop is not None and self.stop.is_set()) or
                    (self.waiting is not None and waiting_for_foreground(self.waiting)))
    def __call__(self):
        if self.is_set():raise DeferredWork('Background work yielded to collection or shutdown')
        # Release the interpreter regularly, including during a large cached scan.
        time.sleep(0.001)


@contextmanager
def background(root, season, stop=None):
    path,waiting=lock_paths(root,season)
    if waiting_for_foreground(waiting):raise DeferredWork('Collector is waiting for this season')
    with FileLock(str(path),timeout=0):
        check=Checkpoint(stop,waiting);check()
        yield check

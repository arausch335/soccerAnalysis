"""Bounded access to JSON coordination files replaced by background threads."""
import json
import os
from pathlib import Path
import time

IS_WINDOWS=os.name=='nt'
ATTEMPTS=12
_MISSING=object()


class SharedFileUnavailable(OSError):
    """Keep saved state intact and defer work until the file becomes readable."""


def _sharing_error(error):
    return (getattr(error,'winerror',None) in (5,32,33) or
            IS_WINDOWS and isinstance(error,PermissionError))


def _unavailable(path,error):
    return SharedFileUnavailable(error.errno,'Shared local file is temporarily inaccessible',str(path))


def read_json(path,*,missing=_MISSING):
    path=Path(path)
    for attempt in range(ATTEMPTS):
        try:
            # Reopen each time: never reuse an old instruction or origin snapshot.
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            if missing is not _MISSING:return missing
            raise
        except OSError as exc:
            if _sharing_error(exc) and attempt<ATTEMPTS-1:
                time.sleep(min(0.02*2**attempt,0.25))
                continue
            if isinstance(exc,PermissionError) or _sharing_error(exc):
                raise _unavailable(path,exc) from exc
            raise


def write_json(path,value,**kwargs):
    from .pandasStore import atomic_json
    try:
        # Atomic replacement already has bounded Windows sharing retries.
        atomic_json(path,value,**kwargs)
    except OSError as exc:
        if isinstance(exc,PermissionError) or _sharing_error(exc):
            raise _unavailable(path,exc) from exc
        raise

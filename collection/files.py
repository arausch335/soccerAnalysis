"""Atomic CSV output and UTC timestamps for collection/export."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4


from storage.pandasStore import atomic_replace


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='') as stream:
            frame.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

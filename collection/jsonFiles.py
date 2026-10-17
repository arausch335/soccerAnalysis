"""Small invalidation-safe cache for immutable provider JSON during one export."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=64)
def _read(path,modified_ns,size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_json(path):
    path=Path(path);stat=path.stat()
    return _read(str(path.resolve()),stat.st_mtime_ns,stat.st_size)


def clear_json_cache():
    _read.cache_clear()

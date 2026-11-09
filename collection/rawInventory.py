"""Confirm exact provider files on the archive and preserve copies before pruning."""
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from filelock import Timeout

from .maintenance import DeferredWork, background
from .removedCollections import removed_seasons
from .storageTransfer import MAX_FILE, check_hash, digest, identifier, safe_path
from .workerIdentity import read_worker_id

FORMAT = 'soccer-raw-inventory-v1'
ROUTE = '/v1/raw-inventory'
BATCH_FILES = 256
BATCH_BYTES = 128 * 1024**2
REQUEST_BYTES = 128 * 1024
PROVIDERS = ('sofascore', 'fotmob', 'whoscored')


def raw_path(relative):
    parts = safe_path(relative, 'raw').split('/')
    if (len(parts) != 5 or parts[2] != 'raw' or parts[3] not in PROVIDERS
            or not re.fullmatch(r'[0-9a-f]{64}\.json', parts[4])):
        raise ValueError('Only saved provider raw response paths are eligible')
    return '/'.join(parts[:2])


def regular_file(root, relative):
    """Do not follow linked or redirected directories into another archive."""
    path = Path(root)
    parts = relative.split('/')
    try:
        for index, part in enumerate(parts):
            path = path / part
            info = path.lstat()
            if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return None
            expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
            if not expected(info.st_mode):
                return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    return path, info


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot(root, relative, *, size=None):
    found = regular_file(root, relative)
    if not found or (size is not None and found[1].st_size != size):
        return None
    path, before = found
    try:
        sha = digest(path)
    except FileNotFoundError:
        return None
    after = regular_file(root, relative)
    if not after or signature(before) != signature(after[1]):
        return None
    return {'path': relative, 'size': before.st_size, 'sha256': sha}, before


def _retain(root, relative, row):
    # Provider files are replaced atomically. A hard link keeps the verified
    # version on MSI even when a later collection replaces its active path.
    vault = Path(root) / '_system/storage/retained-raw'
    vault.mkdir(parents=True, exist_ok=True)
    saved = vault / row['sha256']
    if not saved.exists():
        staged = vault / (uuid4().hex + '.tmp')
        try:
            os.link(Path(root) / relative, staged)
            verified = snapshot(root, staged.relative_to(root).as_posix(), size=row['size'])
            if not verified or verified[0]['sha256'] != row['sha256']:
                raise OSError('Raw file changed before MSI could retain it')
            try:
                os.link(staged, saved)
                return
            except FileExistsError:
                pass
        finally:
            staged.unlink(missing_ok=True)
    verified = snapshot(root, saved.relative_to(root).as_posix(), size=row['size'])
    if not verified or verified[0]['sha256'] != row['sha256']:
        raise OSError('Retained raw copy did not verify')


def verify_batch(root, request):
    if (not isinstance(request, dict) or request.get('format') != FORMAT
            or type(request.get('retain')) is not bool):
        raise ValueError('Invalid raw inventory request')
    rows = request.get('files')
    if not isinstance(rows, list) or not 0 < len(rows) <= BATCH_FILES:
        raise ValueError('Invalid raw inventory batch size')
    seasons = set()
    paths = set()
    size = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256'}:
            raise ValueError('Invalid raw inventory file')
        seasons.add(raw_path(row['path']))
        paths.add(row['path'])
        check_hash(row['sha256'])
        if type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE:
            raise ValueError('Invalid raw inventory file size')
        size += row['size']
    if len(seasons) != 1 or len(paths) != len(rows) or (len(rows) > 1 and size > BATCH_BYTES):
        raise ValueError('Raw inventory batches must cover one season')
    season = next(iter(seasons))
    if not (Path(root) / '.soccer-scraper.json').is_file():
        raise ValueError('Storage root is not a marked data archive')
    reply = {'format': FORMAT, 'request': identifier(request), 'worker': read_worker_id(root),
             'matches': [], 'busy': False}
    if season in removed_seasons():
        return reply
    try:
        with background(root, season) as check:
            matches = []
            for row in rows:
                check()
                current = snapshot(root, row['path'], size=row['size'])
                if not current or current[0] != row:
                    continue
                if request['retain']:
                    _retain(root, row['path'], row)
                matches.append(row['path'])
            reply['matches'] = matches
    except (Timeout, DeferredWork):
        reply['busy'] = True
    return reply

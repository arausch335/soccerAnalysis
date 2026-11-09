"""Compare Main's saved provider files with live MSI copies over private storage."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from collection.rawInventory import (BATCH_BYTES, BATCH_FILES, FORMAT, ROUTE, raw_path,
                                     regular_file, signature, snapshot)
from collection.removedCollections import removed_seasons
from collection.storageConfig import path_for
from collection.storageTransfer import MAX_FILE, Peer, identifier
from collection.workerIdentity import read_worker_id
from collection.workerRuntime import now
from storage.layout import system_path
from storage.pandasStore import atomic_json


def _batches(root, directory):
    batch = []
    size = 0
    for path in sorted(directory.glob('*/*.json')):
        relative = path.relative_to(root).as_posix()
        try:
            raw_path(relative)
        except ValueError:
            continue
        saved = snapshot(root, relative)
        if saved is None or saved[0]['size'] > MAX_FILE:
            continue
        if batch and (len(batch) >= BATCH_FILES or size + saved[0]['size'] > BATCH_BYTES):
            yield batch
            batch = []
            size = 0
        batch.append(saved)
        size += saved[0]['size']
    if batch:
        yield batch


def _match_batch(peer, hub, batch, retain):
    request = {'format': FORMAT, 'retain': retain, 'files': [row for row, _ in batch]}
    reply = peer.request(ROUTE, request, timeout=60)
    matches = reply.get('matches') if isinstance(reply, dict) else None
    paths = {row['path'] for row, _ in batch}
    if (not isinstance(matches, list) or any(not isinstance(path, str) for path in matches)
            or not set(matches) <= paths or reply.get('format') != FORMAT
            or reply.get('worker') != hub or reply.get('request') != identifier(request)
            or type(reply.get('busy')) is not bool or (reply['busy'] and matches)):
        raise ValueError('MSI returned invalid raw inventory evidence')
    return set(matches), reply['busy']


def compare(root, peer, hub, *, apply=False, report=print):
    root = Path(root)
    excluded = removed_seasons()
    confirmed = []
    total = busy = 0
    with ExitStack() as stack:
        if apply:
            for name in ('.worker.lock', '.collector.lock'):
                stack.enter_context(FileLock(str(system_path(root, name)), timeout=0))
        for directory in sorted(root.glob('*/*/raw')):
            season = directory.parent.relative_to(root).as_posix()
            if season in excluded:
                continue
            scanned = matched = waiting = 0
            for batch in _batches(root, directory):
                paths, deferred = _match_batch(peer, hub, batch, apply)
                scanned += len(batch)
                matched += len(paths)
                if deferred:
                    waiting += len(batch)
                confirmed.extend((row, info) for row, info in batch if row['path'] in paths)
            if scanned:
                note = f' ({waiting:,} skipped while MSI season was busy)' if waiting else ''
                report(f'{season}  {matched:,}/{scanned:,} exact copies{note}', flush=True)
            total += scanned
            busy += waiting
        estimated = sum(getattr(info, 'st_blocks', info.st_size / 512) * 512
                        for _, info in confirmed if info.st_nlink == 1)
        report(f'{len(confirmed):,}/{total:,} exact copies, estimated {estimated / 1024**3:.2f} GiB reclaimable.',
               flush=True)
        if busy:
            report(f'{busy:,} files were skipped. Retry when MSI is idle.', flush=True)
        if not apply or not confirmed:
            return
        receipt = root / '_system/storage/raw-prune' / (uuid4().hex + '.json')
        evidence = {'format': FORMAT, 'worker': hub, 'checked_at': now(),
                    'files': [row for row, _ in confirmed]}
        atomic_json(receipt, evidence)
        report(f'MSI recovery receipt saved at {receipt}', flush=True)
        removed = 0
        for row, original in confirmed:
            current = regular_file(root, row['path'])
            if current and signature(current[1]) == signature(original):
                current[0].unlink()
                removed += 1
        atomic_json(receipt, {**evidence, 'finished_at': now(), 'removed_count': removed})
        report(f'Removed {removed:,} raw files from Main. Changed files were kept.', flush=True)


def main(argv=None):
    import config
    from config.workerPlan import load_plan
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--apply', action='store_true',
                        help='Remove only verified copies after MSI retains them. Stop Main first.')
    args = parser.parse_args(argv)
    root = args.data_root.resolve()
    try:
        plan = load_plan()
        worker = read_worker_id(root)
        if (not (root / '.soccer-scraper.json').is_file() or worker != plan.coordinator
                or plan.workers[worker].get('role') != 'monitor'):
            raise ValueError('Run this on the marked Main monitor data folder')
        options = json.loads(path_for(root).read_text(encoding='utf-8'))
        if (not plan.storage['enabled'] or options['role'] != 'consumer'
                or options['worker_id'] != worker or options['hub'] != plan.storage['hub']
                or options['url'] != plan.storage['url']):
            raise ValueError('Main storage settings do not match the worker plan')
        peer = Peer(options['url'], options['token'])
        peer.request('/v1/status', timeout=5)
        compare(root, peer, options['hub'], apply=args.apply)
    except Timeout:
        parser.exit(2, 'Stop Main and any local collection commands before applying cleanup.\n')
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'Raw inventory stopped: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

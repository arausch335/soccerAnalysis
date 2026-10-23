"""Compiled fixtures go directly to Main; the MSI raw outbox is separate."""
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

from filelock import FileLock

from storage.pandasStore import atomic_json
from .storageServer import Archive
from .storageTransfer import (CHUNK, MAX_MANIFEST, FIXTURE_DATASET_PATHS, Outbox,
                              Peer, check_hash, encode, identifier, link_or_copy,
                              validate_manifest)
from .workerRuntime import now


def receipt_path(root):
    return Path(root) / '_system/compiled/receipt.json'


def fixture_files(output, *, fixture_csv=None):
    output = Path(output)
    files = {name: output / name for name in FIXTURE_DATASET_PATHS}
    if fixture_csv is not None:
        files['fixtures.csv'] = Path(fixture_csv)
    if not all(path.is_file() and not path.is_symlink() for path in files.values()):
        raise ValueError('Compile fixtures and all concordance files before sending')
    return files


class CompiledPeer(Peer):
    def request(self, path, *args, **kwargs):
        if not path.startswith('/v1/uploads'):
            raise ValueError('Compiled delivery cannot access raw storage')
        return super().request('/v1/compiled' + path[len('/v1'):], *args, **kwargs)


def outbox(root, worker):
    return Outbox(root, worker, directory=Path(root) / '_system/compiled/outbox')


def flush(root, *, stop=None):
    """Resume compiled uploads through the worker's Main reporting credentials."""
    if not (Path(root) / '_system/compiled/outbox/queue.sqlite').is_file():
        return 0
    from .reporting import load_local
    from .workerIdentity import read_worker_id
    options = load_local(root)
    if not options or options['mode'] != 'client' or not options.get('url'):
        return 0
    worker = read_worker_id(root)
    if worker != options['worker_id']:
        raise ValueError('Compiled delivery reporting identity differs')
    from storage.layout import system_path
    identity = json.loads(system_path(root, 'identity.json').read_text(encoding='utf-8'))
    peer = CompiledPeer(options['url'], options['token'], headers={'X-Worker-Instance': identity['instance_id']})
    queue = outbox(root, worker)
    sent = 0
    with FileLock(str(queue.directory / '.send.lock'), timeout=0):
        while queue.queued() and not (stop is not None and stop.is_set()):
            if not queue.send_one(peer, stop=stop):
                break
            sent += 1
    return sent


def deliver(root, output, *, fixture_csv=None, attempt=True, report=print):
    from .reporting import load_local
    from .workerIdentity import read_worker_id
    options = load_local(root)
    if options and options['mode'] == 'server':
        return {'state': 'local_on_main'}
    worker = read_worker_id(root)
    if worker == 'legacy-unidentified':
        report('Compiled fixtures saved locally. Configure this worker identity and Main reporting before delivery.', flush=True)
        return {'state': 'setup_pending'}
    mid = outbox(root, worker).enqueue(fixture_files(output, fixture_csv=fixture_csv), kind='dataset')
    # Always queue first. Missing credentials and offline Main never lose a build.
    try:
        if attempt:flush(root)
    except (OSError, ValueError, sqlite3.Error):
        pass
    queue = outbox(root, worker)
    with queue.connect() as db:
        state = db.execute('SELECT state FROM queue WHERE id=?', (mid,)).fetchone()[0]
    delivered = state == 'sent'
    report('Compiled fixtures installed on Main directly.' if delivered else
           'Compiled fixtures queued for direct delivery to Main; start/restart this worker to retry in the background.', flush=True)
    return {'state': 'installed_on_main' if delivered else 'queued', 'id': mid}


class CompiledReceiver(Archive):
    def __init__(self, root, output, *, reserve=1024**3):
        self.root, self.output = Path(root), Path(output)
        super().__init__(root, reserve=reserve, directory=Path(root) / '_system/compiled/receiver')

    def register(self, worker, manifest):
        validate_manifest(manifest)
        paths = {f['path'] for f in manifest['files']}
        if manifest['kind'] != 'dataset' or manifest['worker'] != worker or paths != FIXTURE_DATASET_PATHS:
            raise PermissionError('Only owned compiled fixture bundles may be delivered to Main')
        mid = identifier(manifest)
        with self.lock, self.connect() as db:
            db.execute('INSERT OR IGNORE INTO uploads VALUES(?,?,?,?,?)',
                       (mid, worker, encode(manifest).decode(), 'pending', None))
            db.commit()
        return {'id': mid}

    def commit(self, worker, mid):
        # The final ACK means installed on Main, not merely uploaded.
        with FileLock(str(self.directory / '.install.lock'), timeout=25):
            result = super().commit(worker, mid)
            if result['state'] == 'imported':
                return result
            manifest, _ = self.upload(worker, mid)
            receiver = self
            class Objects:
                def download(self, sha, target):
                    link_or_copy(receiver.objects / check_hash(sha), target)
            from .storageAgent import install_dataset
            install_dataset(Objects(), manifest, self.output, self.directory / 'downloads')
            atomic_json(receipt_path(self.root), {'id': mid, 'worker': worker, 'received_at': now(), 'route': 'worker_to_main'})
            self.incorporated(mid, manifest)
            self.collected()
            return {'id': mid, 'state': 'imported'}

    def dispatch(self, handler, worker, store):
        """Called only after the reporting server authenticates the worker token."""
        try:
            if handler.headers.get('Origin') or handler.headers.get('Transfer-Encoding'):
                raise PermissionError('Browser/chunked requests are not accepted')
            instance = handler.headers.get('X-Worker-Instance')
            if not instance:
                raise PermissionError('Worker instance is required')
            with store.lock, store.connect() as db:
                row = db.execute('SELECT instance FROM workers WHERE worker=?', (worker,)).fetchone()
            if row and row['instance'] and row['instance'] != instance:
                raise PermissionError('Worker instance differs')
            url = urlsplit(handler.path)
            parts = url.path.strip('/').split('/')
            def body(limit):
                size = int(handler.headers.get('Content-Length', '-1'))
                if not 0 <= size <= limit:
                    raise ValueError('Invalid compiled upload size')
                value = handler.rfile.read(size)
                if len(value) != size:
                    raise ValueError('Truncated compiled upload')
                return value
            if handler.command == 'POST' and parts == ['v1', 'compiled', 'uploads']:
                result = self.register(worker, json.loads(body(MAX_MANIFEST)))
            elif len(parts) == 6 and parts[:3] == ['v1', 'compiled', 'uploads'] and parts[4] == 'blobs':
                if handler.command == 'GET':
                    with self.lock:
                        result = self.offset(worker, parts[3], parts[5])
                elif handler.command == 'PUT':
                    result = self.chunk(worker, parts[3], parts[5], int(parse_qs(url.query)['offset'][0]), body(CHUNK))
                else:
                    handler.reply(404, {'error': 'Unknown compiled endpoint'}); return
            elif handler.command == 'POST' and len(parts) == 5 and parts[:3] == ['v1', 'compiled', 'uploads'] and parts[4] == 'commit':
                body(MAX_MANIFEST)
                result = self.commit(worker, parts[3])
            else:
                handler.reply(404, {'error': 'Unknown compiled endpoint'}); return
            handler.reply(200, result)
        except PermissionError:
            handler.reply(403, {'error': 'Compiled delivery not authorized'})
        except (ValueError, KeyError, TypeError, UnicodeError):
            handler.reply(400, {'error': 'Invalid compiled upload'})
        except (OSError, sqlite3.Error):
            handler.reply(503, {'error': 'Compiled delivery unavailable; files remain queued'})


def main(argv=None):
    import argparse
    import config
    from .workerIdentity import read_worker_id
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--dataset-root', type=Path, default=config.DATASET_ROOT)
    parser.add_argument('--retry-only', action='store_true', help='Send already queued bundles without creating a new snapshot')
    args = parser.parse_args(argv)
    try:
        if args.retry_only:
            sent = flush(args.data_root)
            queue = args.data_root / '_system/compiled/outbox/queue.sqlite'
            remaining = outbox(args.data_root, read_worker_id(args.data_root)).queued() if queue.exists() else 0
            print(f'Compiled bundles installed on Main: {sent}; still queued: {remaining}', flush=True)
            return 2 if remaining else 0
        result = deliver(args.data_root, args.dataset_root)
        return 0 if result['state'] in ('installed_on_main', 'local_on_main') else 2
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'Compiled delivery pending ({type(exc).__name__}); saved files and queued bundles are retained.', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

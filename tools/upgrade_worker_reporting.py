"""One-time safe upgrade from tracked worker snapshots to private reporting.

Run from the repository, after stopping its scraper. Only generated JSON paths
are restored, after verified local backups. Settings, source edits, identities,
raw data, logs and staged changes are never restored, reset, stashed or pushed.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import subprocess


def git(repository, *args):
    return subprocess.run(['git', *args], cwd=repository, capture_output=True, text=True, check=True).stdout


def preserve(repository):
    repository = Path(repository).resolve()
    if git(repository, 'diff', '--cached', '--name-only').strip():
        raise RuntimeError('Staged changes need review. Nothing was restored; finish reviewing them before upgrading.')
    names = git(repository, 'ls-files', '-z', '--', 'ensemble_data/workers/*.json', 'ensemble_data/runtime/*.json').split('\0')
    names = [name for name in names if name]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = repository/'data/worker_runtime/legacy-git-backup'/stamp
    for name in names:
        source = repository/name
        if not source.exists():
            continue
        if source.is_symlink():
            raise RuntimeError('Worker snapshots must be regular local files')
        relative = Path(name).relative_to('ensemble_data')
        target = backup/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
            raise RuntimeError('Worker snapshot backup failed verification; nothing was restored')
        cache = repository/'ensemble_data/reporting'/relative
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, cache)
    return names, backup


def main():
    repository = Path.cwd()
    names, backup = preserve(repository)
    if names:
        # Git is about to delete these generated files. Their live contents are
        # already preserved locally; restore only these paths so ff-only can run.
        git(repository, 'restore', '--worktree', '--', *names)
        print(f'Preserved worker inventories and status in {backup}', flush=True)
    subprocess.run(['git', 'pull', '--ff-only', 'origin', 'main'], cwd=repository, check=True)
    print('Code updated. Configure reporting using docs/WORKERS.md, then restart the worker.', flush=True)


if __name__ == '__main__':
    main()

"""Read-only Git fetches during collection; fast-forward code only between jobs."""
from pathlib import Path
import os
import subprocess
import threading

from filelock import FileLock


class UpdatePaused(RuntimeError):
    pass


class CodeUpdates:
    def __init__(self, repository, worker, options):
        self.repository = Path(repository).resolve()
        self.options = options
        self.status = {'state': 'disabled' if not options['enabled'] else 'waiting',
                       'message': 'Code and assignment updates apply between saved jobs.'}
        self.stop = threading.Event(); self.mutex = threading.RLock(); self.thread = None
        self.lock = FileLock(str(self.repository / '.git' / 'scraper-sync.lock'), timeout=0)

    def git(self, *args, check=True):
        result = subprocess.run(['git', *args], cwd=self.repository, text=True, capture_output=True,
                                timeout=20, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                                 'GIT_EDITOR': 'true', 'GIT_SEQUENCE_EDITOR': 'true'})
        if check and result.returncode:
            raise UpdatePaused('Git ' + args[0] + ' failed; inspect Git locally. User files are preserved.')
        return result.stdout.strip() if check else result

    @property
    def remote_ref(self):
        return self.options['remote'] + '/' + self.options['branch']

    def fetch(self):
        if not self.options['enabled']:
            return
        with self.mutex:
            try:
                with self.lock:
                    self.git('fetch', '--no-tags', self.options['remote'],
                             '+refs/heads/' + self.options['branch'] + ':refs/remotes/' + self.remote_ref)
                    count = int(self.git('rev-list', '--count', 'HEAD..' + self.remote_ref))
                self.status = {'state': 'update_pending' if count else 'current', 'pending_commits': count,
                               'message': 'Code update waiting for a saved job boundary.' if count else 'Code and shared plan are up to date.'}
            except (UpdatePaused, OSError, subprocess.SubprocessError, TimeoutError) as exc:
                self.status = {'state': 'attention', 'message': str(exc) if isinstance(exc, UpdatePaused) else 'Git check unavailable; collection can continue.'}

    def start(self):
        if self.options['enabled']:
            def poll():
                while not self.stop.is_set():
                    self.fetch(); self.stop.wait(self.options['poll_seconds'])
            self.thread = threading.Thread(target=poll, name='code-update-check', daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)

    def boundary(self):
        if not self.options['enabled']:
            return []
        with self.mutex:
            try:
                with self.lock:
                    if self.git('symbolic-ref', '--short', 'HEAD') != self.options['branch']:
                        raise UpdatePaused('Automatic updates require branch ' + self.options['branch'] + '.')
                    if self.git('status', '--porcelain', '--untracked-files=normal'):
                        raise UpdatePaused('Local edits or staged files need review; automatic updates are paused. Settings are preserved.')
                    for marker in ('MERGE_HEAD', 'REBASE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD'):
                        if (self.repository / '.git' / marker).exists():
                            raise UpdatePaused('Finish the current Git operation before automatic updates.')
                    self.git('rev-parse', '--verify', self.remote_ref)
                    if int(self.git('rev-list', '--count', self.remote_ref + '..HEAD')):
                        raise UpdatePaused('Local commits need review; automatic updates never push or rebase them.')
                    before = self.git('rev-parse', 'HEAD')
                    self.git('merge', '--ff-only', self.remote_ref)
                    changed = self.git('diff', '--name-only', before, 'HEAD').splitlines()
                    self.status = {'state': 'restart_pending' if changed else 'current', 'pending_commits': 0,
                                   'message': 'Code update saved. Restarting before the next job.' if changed else 'Code and shared plan are up to date.'}
                    return changed
            except (UpdatePaused, OSError, subprocess.SubprocessError, TimeoutError) as exc:
                self.status = {'state': 'attention', 'message': str(exc) if isinstance(exc, UpdatePaused) else 'Git update unavailable; local files are preserved.'}
                return []

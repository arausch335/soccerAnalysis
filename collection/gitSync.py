"""Fetch during collection; change the checkout only at an explicit job boundary."""
from contextlib import contextmanager
from pathlib import Path
import os
import json
import subprocess
import tempfile
import threading

from filelock import FileLock


class SyncPaused(RuntimeError):
    pass


class GitSync:
    def __init__(self, repository, worker, options):
        self.repository=Path(repository).resolve();self.worker=worker;self.options=options
        self.owned=[f'ensemble_data/workers/{worker}.json',f'ensemble_data/runtime/{worker}.json']
        self.status={'state':'disabled' if not options['enabled'] else 'waiting','message':'Updates apply between saved jobs.'}
        self.stop=threading.Event();self.mutex=threading.RLock();self.thread=None
        self.lock=FileLock(str(self.repository/'.git'/'scraper-sync.lock'),timeout=0)

    def git(self,*args,check=True,input=None,env=None):
        result=subprocess.run(['git',*args],cwd=self.repository,text=True,capture_output=True,
            input=input,timeout=90,env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GIT_EDITOR':'true','GIT_SEQUENCE_EDITOR':'true',**(env or {})})
        if check and result.returncode:
            # Git errors can include credential-bearing URLs. Keep them out of shared status.
            raise SyncPaused('Git '+args[0]+' failed; inspect Git locally. No user files were replaced.')
        return result.stdout.strip() if check else result

    @property
    def remote_ref(self):return self.options['remote']+'/'+self.options['branch']

    def fetch(self):
        if not self.options['enabled']:return
        with self.mutex:
            try:
                with self.lock:
                    self.git('fetch','--no-tags',self.options['remote'],
                             '+refs/heads/'+self.options['branch']+':refs/remotes/'+self.remote_ref)
                    pending=self.git('rev-list','--count','HEAD..'+self.remote_ref)
                self.status={'state':'update_pending' if int(pending) else 'current',
                             'pending_commits':int(pending),'message':'Waiting for a saved job boundary.' if int(pending) else 'Up to date. Updates apply between saved jobs.'}
            except (SyncPaused,OSError,subprocess.SubprocessError,TimeoutError) as exc:
                self.status={'state':'attention','message':str(exc) if isinstance(exc,SyncPaused) else 'Git check unavailable; local collection can continue.'}

    def start(self):
        if not self.options['enabled']:return self
        def poll():
            while not self.stop.is_set():
                self.fetch()
                self.stop.wait(self.options['poll_seconds'])
        self.thread=threading.Thread(target=poll,name='git-check',daemon=True);self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=95)

    def _check_clean(self):
        if self.git('symbolic-ref','--short','HEAD')!=self.options['branch']:
            raise SyncPaused('Automatic updates require branch '+self.options['branch']+'.')
        if self.git('diff','--cached','--name-only'):
            raise SyncPaused('Staged changes need your review; automatic updates are paused.')
        for marker in ('MERGE_HEAD','REBASE_HEAD','rebase-merge','rebase-apply','CHERRY_PICK_HEAD'):
            if (self.repository/'.git'/marker).exists():raise SyncPaused('Finish the current Git operation before automatic updates.')
        changed=set(self.git('diff','--name-only').splitlines())
        changed.update(self.git('ls-files','--others','--exclude-standard').splitlines())
        if changed-set(self.owned):
            raise SyncPaused('Local edits need your review. Commit or move them before automatic updates; settings are preserved.')

    @contextmanager
    def _saved_generated_files(self):
        # Only our two generated snapshots may be temporarily restored. Never stash user files.
        changed=set(self.git('diff','--name-only').splitlines())
        changed.update(self.git('ls-files','--others','--exclude-standard').splitlines())
        snapshots={name:(self.repository/name).read_bytes() for name in self.owned if name in changed and (self.repository/name).exists()}
        backup=self.repository/'data'/'worker_runtime'/'git-backup'/self.worker
        for name,data in snapshots.items():
            target=backup/Path(name).parent.name/Path(name).name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        try:
            for name in snapshots:
                if self.git('ls-files','--error-unmatch','--',name,check=False).returncode==0:
                    self.git('restore','--worktree','--',name)
                else:(self.repository/name).unlink()
            yield
        finally:
            for name,data in snapshots.items():
                target=self.repository/name;target.parent.mkdir(parents=True,exist_ok=True)
                temporary=target.with_suffix('.sync-tmp');temporary.write_bytes(data);os.replace(temporary,target)

    def _only_generated_commits(self):
        for commit in self.git('rev-list',self.remote_ref+'..HEAD').splitlines():
            if self.git('show','-s','--format=%s',commit)!=f'Scraper snapshot: {self.worker}':return False
            paths=set(self.git('diff-tree','--no-commit-id','--name-only','-r',commit).splitlines())
            if not paths or paths-set(self.owned):return False
        return True

    @property
    def receipts(self):return self.repository/'.git'/('scraper-published-'+self.worker+'.json')

    def _receipt_ids(self):
        try:return json.loads(self.receipts.read_text(encoding='utf-8'))
        except (OSError,ValueError):return []

    def _remember(self,commit):
        values=[v for v in self._receipt_ids() if v!=commit]+[commit]
        temporary=self.receipts.with_suffix('.tmp')
        temporary.write_text(json.dumps(values[-10:]),encoding='utf-8');os.replace(temporary,self.receipts)

    def _owned_changes_allowed(self,pending):
        names=set(pending)&set(self.owned)
        if not names:return True
        def blob(ref,name):
            result=self.git('rev-parse','--verify',ref+':'+name,check=False)
            return result.stdout.strip() if not result.returncode else None
        for commit in self._receipt_ids():
            if all(blob(commit,name)==blob(self.remote_ref,name) for name in names):
                self._remember(commit);return True
        return False

    def publish(self):
        """Publish saved snapshots through a private Git index. Never switch running code."""
        if not self.options['enabled'] or not self.options['publish_inventory']:return False
        with self.mutex:
            try:
                with self.lock:
                    self._check_clean()
                    for attempt in range(3):
                        self.git('fetch','--no-tags',self.options['remote'],
                            '+refs/heads/'+self.options['branch']+':refs/remotes/'+self.remote_ref)
                        ahead=int(self.git('rev-list','--count',self.remote_ref+'..HEAD'))
                        if ahead and not self._only_generated_commits():raise SyncPaused('Local source commits need review before snapshot publication.')
                        pending=self.git('diff','--name-only','HEAD...'+self.remote_ref).splitlines()
                        if not self._owned_changes_allowed(pending):raise SyncPaused('Another checkout changed this worker’s snapshots; check its unique worker ID.')
                        base=self.git('rev-parse',self.remote_ref)
                        with tempfile.TemporaryDirectory(prefix='snapshot-',dir=self.repository/'.git') as folder:
                            env={'GIT_INDEX_FILE':str(Path(folder)/'index')}
                            self.git('read-tree',base,env=env)
                            for name in self.owned:
                                path=self.repository/name
                                if not path.exists():continue
                                if path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=self.repository.parent):
                                    raise SyncPaused('Snapshot paths must be regular local files.')
                                saved=json.loads(path.read_text(encoding='utf-8'))
                                if saved.get('worker_id')!=self.worker:raise SyncPaused('Snapshot worker identity differs.')
                                blob=self.git('hash-object','-w','--stdin',input=path.read_text(encoding='utf-8'))
                                self.git('update-index','--add','--cacheinfo','100644',blob,name,env=env)
                            tree=self.git('write-tree',env=env)
                            if tree==self.git('rev-parse',base+'^{tree}'):return True
                            commit=self.git('commit-tree',tree,'-p',base,input=f'Scraper snapshot: {self.worker}\n')
                        # Record before push so an interrupted response is recoverable on restart.
                        self._remember(commit)
                        result=self.git('push',self.options['remote'],commit+':refs/heads/'+self.options['branch'],check=False)
                        if not result.returncode:
                            self.status={'state':'published','message':'Progress published. Running code is unchanged.'}
                            return True
                        if '[rejected]' not in result.stderr or attempt==2:raise SyncPaused('Snapshot push failed; progress stays saved locally.')
            except (SyncPaused,OSError,ValueError,subprocess.SubprocessError,TimeoutError) as exc:
                self.status={'state':'attention','message':str(exc) if isinstance(exc,SyncPaused) else 'Snapshot publication unavailable; progress stays saved locally.'}
        return False

    def boundary(self):
        """Return changed code/config paths. A failure leaves local edits and data intact."""
        if not self.options['enabled']:return []
        with self.mutex:
            try:
                with self.lock:
                    self._check_clean()
                    self.git('rev-parse','--verify',self.remote_ref)
                    before=self.git('rev-parse','HEAD')
                    pending=set(self.git('diff','--name-only','HEAD...'+self.remote_ref).splitlines())
                    if not self._owned_changes_allowed(pending):
                        raise SyncPaused('Another checkout changed this worker’s snapshots. Confirm that each computer has a unique worker ID.')
                    with self._saved_generated_files():
                        ahead=int(self.git('rev-list','--count',self.remote_ref+'..HEAD'))
                        if ahead:
                            if not self._only_generated_commits():raise SyncPaused('Local commits need review before automatic updates.')
                            result=self.git('rebase',self.remote_ref,check=False)
                            if result.returncode:
                                self.git('rebase','--abort',check=False)
                                raise SyncPaused('Snapshot update conflicted; automatic rebase was cancelled.')
                        else:self.git('merge','--ff-only',self.remote_ref)
                    changed=self.git('diff','--name-only',before,'HEAD').splitlines()
                    code=[name for name in changed if not name.startswith('ensemble_data/')]
                    # Do not publish data or source edits: only this worker’s two snapshots.
                    if self.options['publish_inventory']:
                        existing=[name for name in self.owned if (self.repository/name).exists()]
                        if existing:
                            self.git('add','--',*existing)
                            if self.git('diff','--cached','--name-only'):
                                staged=set(self.git('diff','--cached','--name-only').splitlines())
                                if staged-set(self.owned):raise SyncPaused('New staged edits need review; snapshots were not published.')
                                try:self.git('commit','--only','-m',f'Scraper snapshot: {self.worker}','--',*existing)
                                except SyncPaused:
                                    self.git('reset','--quiet','--',*existing,check=False)
                                    raise
                        if int(self.git('rev-list','--count',self.remote_ref+'..HEAD')):
                            if not self._only_generated_commits():raise SyncPaused('Source commits require an explicit push; automatic publication stopped.')
                            self.git('push',self.options['remote'],'HEAD:refs/heads/'+self.options['branch'])
                    self.status={'state':'restart_pending' if code else 'current',
                                 'pending_commits':0,
                                 'message':'Update saved. Restarting before the next job.' if code else 'Up to date. Updates apply between saved jobs.'}
                    return code
            except (SyncPaused,OSError,subprocess.SubprocessError,TimeoutError) as exc:
                # If update succeeded but publication failed, a restart is still required.
                self.status={'state':'attention','message':str(exc) if isinstance(exc,SyncPaused) else 'Git sync unavailable; local files are preserved.'}
                return locals().get('code',[])

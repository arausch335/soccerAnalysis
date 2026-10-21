"""Bounded shutdown of Python collectors belonging to this checkout only."""
import argparse
from datetime import datetime,timezone
import os
from pathlib import Path
import signal
import sys


def option(args,name,default=None):
    for i,value in enumerate(args):
        if value==name and i+1<len(args):return args[i+1]
        if value.startswith(name+'='):return value.split('=',1)[1]
    return default


def inside(path,root):
    path=Path(path).resolve();root=Path(root).resolve()
    return path==root or root in path.parents


def project_command(args,cwd,repository):
    """Recognize scripts, module launches and PyCharm --file arguments."""
    repository=Path(repository).resolve()
    candidates=[value for i,value in enumerate(args) if i and args[i-1]=='--file']
    skip=False
    for value in args[1:]:
        if skip:skip=False;continue
        if value in ('-X','-W'):skip=True;continue
        if value in ('-c','-m'):break
        if value.startswith('-'):continue
        candidates.append(value);break
    for value in candidates:
        if value.endswith('.py') or Path(value).name in ('soccer-analysis','soccer-fixtures','soccer-dataset','soccer-data-review'):
            path=Path(value)
            if not path.is_absolute():
                if not cwd:continue
                path=Path(cwd)/path
            if inside(path,repository):return True
    if '-m' in args and cwd and inside(cwd,repository):
        i=args.index('-m')
        module=args[i+1] if i+1<len(args) else ''
        return module in ('main','dataset_builder','review_data','api_test') or module.startswith(('collection.','fixtures.','tools.','dashboard.'))
    return False


def process_context(args,cwd,default_root,repository):
    root=Path(option(args,'--data-root',str(default_root))).expanduser()
    if not root.is_absolute():root=Path(cwd or repository)/root
    root=root.resolve()
    worker=option(args,'--worker-id')
    if worker is None:
        from .workerIdentity import read_worker_id
        try:worker=read_worker_id(root)
        except (OSError,ValueError):worker='unknown'
    ensemble=Path(option(args,'--ensemble-root',str(Path(repository)/'ensemble_data'))).expanduser()
    if not ensemble.is_absolute():ensemble=Path(cwd or repository)/ensemble
    return root,worker,ensemble.resolve()


def discover(repository,default_root,*,worker=None,data_root=None):
    import psutil
    excluded={os.getpid(),*[p.pid for p in psutil.Process().parents()]}
    targets=[]
    for process in psutil.process_iter(['pid','name','cmdline','cwd']):
        try:
            if process.pid in excluded:continue
            name=(process.info['name'] or '').lower()
            if not name.startswith(('python','pypy')):continue
            args=process.info['cmdline'] or [];cwd=process.info['cwd']
            if not project_command(args,cwd,repository):continue
            root,identity,ensemble=process_context(args,cwd,default_root,repository)
            if worker is not None and identity!=worker:continue
            if data_root is not None and root!=Path(data_root).resolve():continue
            targets.append((process,root,identity,ensemble))
        except (psutil.NoSuchProcess,psutil.ZombieProcess):continue
        except psutil.AccessDenied:
            # An inaccessible process is not guessed to belong to this project.
            continue
    return targets


def _stop(processes,timeout,report):
    import psutil
    selected={p.pid:p for p in processes};forced=set()
    for process in list(selected.values()):
        try:
            # Include Python children even if they execute outside this checkout.
            for child in process.children(recursive=True):
                if child.name().lower().startswith(('python','pypy')):selected[child.pid]=child
        except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    for process in selected.values():
        try:
            report(f'Stopping project Python process {process.pid}...',flush=True)
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGTERM)
        except psutil.NoSuchProcess:pass
        except (psutil.AccessDenied,OSError,ValueError):
            try:process.terminate();forced.add(process.pid)
            except psutil.NoSuchProcess:pass
            except psutil.AccessDenied as exc:raise OSError(f'Permission denied stopping project process {process.pid}') from exc
    _,alive=psutil.wait_procs(list(selected.values()),timeout=timeout)
    for process in alive:
        try:
            report(f'Process {process.pid} did not stop; forcing termination.',flush=True)
            process.kill();forced.add(process.pid)
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied as exc:raise OSError(f'Permission denied stopping project process {process.pid}') from exc
    _,alive=psutil.wait_procs(alive,timeout=3)
    return alive,forced


def _save_stopped(targets):
    if not targets:return
    import json
    from storage.layout import system_path
    from storage.pandasStore import atomic_json
    grouped={}
    for process,root,worker,ensemble in targets:
        grouped.setdefault((root,worker,ensemble),set()).add(process.pid)
    for (root,worker,ensemble),pids in grouped.items():
        path=system_path(root,'worker_runtime')/(worker+'.json')
        if not path.exists():continue
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved.get('process_id') is not None and saved['process_id'] not in pids:continue
        stamp=datetime.now(timezone.utc).isoformat()
        saved.update(state='stopped',current=None,provider_active=False,updated_at=stamp,job_ended_at=saved.get('job_ended_at') or stamp)
        atomic_json(path,saved);atomic_json(ensemble/'runtime'/(worker+'.json'),saved)


def terminate(repository,default_root,*,worker=None,data_root=None,timeout=10,report=print):
    from filelock import FileLock,Timeout
    targets=[];forced=set();failed=[]
    for attempt in range(3):
        found=discover(repository,default_root,worker=worker,data_root=data_root)
        if not found:break
        targets.extend(found)
        try:
            alive,killed=_stop([t[0] for t in found],timeout,report)
            forced.update(killed)
            if alive:failed.extend(p.pid for p in alive);break
        except (OSError,ValueError) as exc:
            report('Could not stop a project process: '+str(exc),flush=True);return 2
    remaining=discover(repository,default_root,worker=worker,data_root=data_root)
    if remaining or failed:
        report('Project processes still running: '+', '.join(str(p) for p in sorted(set(failed+[t[0].pid for t in remaining]))),flush=True)
        return 2
    roots={t[1] for t in targets}
    if worker is None or data_root is not None:roots.add(Path(data_root or default_root).resolve())
    for root in roots:
        for relative in ('_system/locks/worker.lock','_system/locks/collector.lock','.worker.lock','.collector.lock'):
            path=root/relative
            if not path.exists():continue
            try:
                with FileLock(str(path),timeout=0):pass
            except Timeout:
                report('A data lock is still held: '+str(path)+'. No lock files were deleted.',flush=True)
                return 2
    try:_save_stopped(targets)
    except (OSError,ValueError) as exc:
        report('Processes stopped, but saved status could not be updated: '+str(exc),flush=True)
        return 2
    report(f'Verified: no matching project Python processes remain; data locks are released. '
           f'{len({t[0].pid for t in targets})} stopped; {len(forced)} required forced termination.',flush=True)
    return 0


def install_shutdown_handlers():
    """Let OS-delivered stop requests use the existing checkpoint-safe cleanup."""
    def interrupt(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupt)
    if hasattr(signal,'SIGBREAK'):signal.signal(signal.SIGBREAK,interrupt)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--terminate',action='store_true',required=True)
    parser.add_argument('--worker-id',help='Stop only this worker; default stops all project Python workers/monitors')
    parser.add_argument('--data-root',type=Path,help='Restrict shutdown to this data folder')
    parser.add_argument('--timeout',type=float,default=10,help='Grace period before forcing shutdown (0-30 seconds)')
    args=parser.parse_args(argv)
    if not 0<=args.timeout<=30:parser.error('--timeout must be between 0 and 30 seconds')
    try:import psutil
    except ImportError:
        print('Termination requires psutil. Run python -m pip install "psutil>=5.9,<8", then retry.');return 2
    repository=Path(__file__).resolve().parent.parent
    default_root=repository/'data'
    try:
        import config
        repository=Path(config.PROJECT_ROOT);default_root=Path(config.DATA_ROOT)
    except Exception:
        print('Settings could not be read. Using data/ for default lock checks; supply --data-root for a custom folder.')
    return terminate(repository,default_root,worker=args.worker_id,data_root=args.data_root,timeout=args.timeout)

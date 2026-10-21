"""League seasons stay at the data root; machine state lives under _system."""
from contextlib import ExitStack
from pathlib import Path
import os
from filelock import FileLock,Timeout

STATE_PATHS={
 '.ensemble-computer.json':'config/legacy-identity.json',
 'identity.json':'config/identity.json','reporting.json':'config/reporting.json',
 'worker_runtime':'workers','.ensemble-origins.json':'provenance.json',
 'safety':'network/pacing','http_sessions':'network/sessions',
 'fixture_schedules':'fixtures','ensemble_imports':'imports',
 'extension_diagnostics':'logs/extension','browser_diagnostics':'logs/retired-browser',
 'cache':'cache','.worker.lock':'locks/worker.lock','.collector.lock':'locks/collector.lock',
 'actions':'analysis/actions','events':'analysis/events','vaep':'analysis/vaep',
 'reference.csv':'analysis/reference.csv',
 'http_cache':'retired_cache/http_cache','browser_cache':'retired_cache/browser_cache',
 'extension_cache':'retired_cache/extension_cache','browser_profiles':'retired_browser_profiles',
}

def system_path(root,name):
    path=Path(root)/'_system'/STATE_PATHS[name]
    if name in ('.worker.lock','.collector.lock'):path.parent.mkdir(parents=True,exist_ok=True)
    return path

def organize_data_root(root):
    """Move known machine state once, before any collector/server opens it.

    Conflicting destinations are never overwritten. Legacy transport caches are
    handled separately by tools.compact_cache so unique responses can be checked.
    """
    root=Path(root)
    old=[name for name in STATE_PATHS if (root/name).exists() and not name.endswith('.lock')]
    if not old:
        return []
    # Existing worker/collector processes keep their old file handles. Defer
    # migration until they have exited rather than moving files underneath them.
    with ExitStack() as stack:
        # Old running versions must be stopped before their open files can move.
        for lock in ('.worker.lock','.collector.lock'):
            stack.enter_context(FileLock(str(root/lock),timeout=0))
        for lock in ('.worker.lock','.collector.lock'):
            target=system_path(root,lock);target.parent.mkdir(parents=True,exist_ok=True)
            stack.enter_context(FileLock(str(target),timeout=0))
        for name in old:
            target=system_path(root,name)
            if target.exists():raise ValueError(f'Layout migration conflict: {root/name} and {target}; inspect both before continuing')
        moved=[]
        for name in old:
            target=system_path(root,name);target.parent.mkdir(parents=True,exist_ok=True)
            os.replace(root/name,target);moved.append(name)
    for lock in ('.worker.lock','.collector.lock'):
        (root/lock).unlink(missing_ok=True)
    return moved


def full_path(path):
    """Use native long-path support for deeply nested Windows checkpoint files."""
    path=Path(path).expanduser().resolve()
    value=str(path)
    if os.name=='nt' and not value.startswith('\\\\?\\'):
        value=('\\\\?\\UNC\\'+value[2:]) if value.startswith('\\\\') else '\\\\?\\'+value
        return Path(value)
    return path

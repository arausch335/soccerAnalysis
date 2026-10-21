"""Keep Main's local holdings current without running a collector."""
from datetime import datetime, timezone
from pathlib import Path
import threading

from filelock import FileLock, Timeout
from storage.layout import system_path
from .ensembleProgress import PortableProgress,read_progress
from .inventoryCache import InventoryInterrupted


class InventoryMonitor:
    def __init__(self,root,ensemble,worker,*,period=30,on_change=None,report=print):
        self.root=Path(root);self.ensemble=Path(ensemble);self.worker=worker
        self.period=period;self.on_change=on_change;self.report=report
        self.stop=threading.Event();self.status={'state':'waiting'};self.thread=None
        self.error=None;self.first=True

    def check(self):
        try:
            with FileLock(str(system_path(self.root,'.collector.lock')),timeout=0):
                self.status={**self.status,'state':'checking'}
                progress=PortableProgress(self.ensemble/'provenance.json',self.root,worker_id=self.worker)
                previous=read_progress(progress.worker_path)['records'] if progress.worker_path.exists() else {}
                progress.scan(cancel=self.stop)
                changed=previous!=progress.local_records
                self.status={'state':'current','checked_at':datetime.now(timezone.utc).isoformat(),
                             **progress.scan_summary,'records':len(progress.local_records)}
                if self.first or progress.scan_summary['validated']:
                    self.report(f'[Inventory] Main: {progress.scan_summary["reused"]} unchanged artifacts reused; '
                                f'{progress.scan_summary["validated"]} new/changed artifacts validated.',flush=True)
                self.first=False;self.error=None
            if changed and self.on_change:self.on_change()
            return changed
        except Timeout:
            self.status={**self.status,'state':'waiting','message':'Collection or import is using the data folder.'}
        except InventoryInterrupted:pass
        except Exception as exc:
            message=str(exc)
            self.status={**self.status,'state':'attention','message':message}
            if message!=self.error:self.report('[Inventory] Main needs review: '+message,flush=True)
            self.error=message
        return False

    def start(self):
        def watch():
            while not self.stop.is_set():
                self.check();self.stop.wait(self.period)
        self.thread=threading.Thread(target=watch,name='inventory-monitor',daemon=True)
        self.thread.start();return self

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=15)

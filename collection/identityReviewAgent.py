"""Optional Main-only profile investigation, separate from collection and reporting."""
from contextvars import copy_context
import sqlite3
import threading
from pathlib import Path

from providers.httpClient import ProviderError
from storage.pandasStore import atomic_json
from .workerRuntime import now


class IdentityReviewAgent:
    def __init__(self,root,bridge,*,on_change=lambda:None,report=print):
        self.root=Path(root);self.bridge=bridge;self.on_change=on_change;self.report=report
        self.stop=threading.Event();self.thread=None
        self.path=self.root/'_system/workers/identity-review.json'
        self.status={};self.extension_checked=False

    def save(self,state,message):
        value={'state':state,'message':message}
        if all(self.status.get(k)==v for k,v in value.items()):return
        self.status={**value,'updated_at':now()};atomic_json(self.path,self.status)

    def step(self):
        if not self.bridge.ready:
            self.save('waiting_for_extension','Connect Main to Soccer Collector using the code printed in its console. Profile checks wait here.')
            return False
        if 'player-profiles-v1' not in getattr(self.bridge,'capabilities',()):
            self.save('update_extension','Reload Soccer Collector in chrome://extensions, then reconnect it to Main. The connected extension does not support player profiles.')
            return False
        from providers.playerProfiles import investigate_queue,retry_legacy_extension_failures
        if not self.extension_checked:
            retry_legacy_extension_failures(self.root);self.extension_checked=True
        self.save('checking','Checking queued player profiles on Main.')
        changed=investigate_queue(self.root,self.report)
        if changed:self.on_change()
        else:self.save('idle','Main is connected. No unchecked player profiles are waiting.')
        return changed

    def start(self):
        from providers.identityReview import seed_existing
        def run():
            try:seed_existing(self.root)
            except (OSError,ValueError,sqlite3.Error) as exc:
                self.report('[Identity review] Could not load older suggestions: '+type(exc).__name__)
            while not self.stop.is_set():
                try:changed=self.step()
                except (ProviderError,OSError,ValueError,sqlite3.Error) as exc:
                    self.save('retrying','Profile checks paused after '+type(exc).__name__+'. Main will retry; collection continues.')
                    changed=False
                self.stop.wait(.25 if changed else 5)
        context=copy_context()  # Reuse Main's identity root and shared extension bridge.
        self.thread=threading.Thread(target=lambda:context.run(run),name='main-identity-review',daemon=True)
        self.save('waiting_for_extension' if not self.bridge.ready else 'starting','Main profile checks enabled.')
        self.thread.start();return self

    def close(self):
        self.stop.set()
        self.bridge.close()  # Wake a pending extension request before joining.
        if self.thread:self.thread.join(timeout=3)
        self.save('stopped','Profile checks stopped. Start Main with --identity-review to resume.')

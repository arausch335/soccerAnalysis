"""Small, allowlisted extension trace: no response bodies or connection secrets."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit, urlunsplit


class ExtensionDiagnostics:
    def __init__(self, directory, report=print):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.path = Path(directory)/f'{stamp}-{secrets.token_hex(4)}.jsonl'
        self.report = report
        self.lock = threading.Lock()
        self.failed = False

    @staticmethod
    def summary(value):
        """Keep only operational metadata, never raw payloads, HTML or headers."""
        keys = {'ok', 'status', 'error', 'operation', 'endpoint', 'tab', 'match_id', 'player_id',
                'timeout_ms', 'delay_min_seconds', 'delay_max_seconds', 'verification',
                'visible_challenge', 'challenge_evidence', 'tab_opened', 'player_opened',
                'recoverable', 'title', 'ready_state', 'document_id', 'match_controls', 'player_panels',
                'dialogs', 'label', 'players', 'player_target', 'actions', 'action',
                'milliseconds', 'content_type', 'browser_load_state', 'navigation_retries', 'navigation_reused',
                'recovery_attempts', 'recovery_reason', 'recovery_stage', 'verification_resolution', 'reload_attempt', 'bypass_cache',
                'next_reload_at', 'wait_seconds', 'diagnostics'}
        result = {}
        for key, item in value.items():
            if key in ('source_url', 'url', 'match_url') and isinstance(item, str):
                parsed = urlsplit(item)
                if parsed.scheme == 'https' and parsed.netloc == 'www.sofascore.com':
                    mid = re.search(r'(?:^|,)id:(\d+)', parsed.fragment)
                    result[key] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '',
                                             'id:'+mid[1] if mid else ''))
            elif key in keys:
                if isinstance(item, dict):result[key] = ExtensionDiagnostics.summary(item)
                elif isinstance(item, list):
                    result[key] = [ExtensionDiagnostics.summary(v) for v in item[:30] if isinstance(v, dict)]
                elif isinstance(item, str):result[key] = item[:500]
                elif item is None or isinstance(item, (int, float, bool)):result[key] = item
        return result

    def record(self, event, *, action=None, args=None, result=None, elapsed=None):
        row = {'time': datetime.now(timezone.utc).isoformat(), 'event': event}
        if action is not None:row['action'] = action
        if args is not None:row['args'] = self.summary(args)
        if result is not None:row['result'] = self.summary(result)
        if elapsed is not None:row['elapsed_seconds'] = round(elapsed, 3)
        from collection.workerLogs import operational_log
        failed = isinstance(result,dict) and (result.get('error') or result.get('ok') is False)
        operational_log('Extension '+self.path.parent.name+' '+json.dumps(row,ensure_ascii=True), 'WARNING' if failed else 'INFO')
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(row, ensure_ascii=True)+'\n')
        except OSError:
            if not self.failed:
                self.failed = True
                self.report('Extension diagnostic log could not be written; collection continues.', flush=True)

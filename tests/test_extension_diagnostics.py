from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest

from providers.transports.diagnostics import ExtensionDiagnostics


class ExtensionDiagnosticTests(unittest.TestCase):
    def test_trace_keeps_operational_evidence_and_excludes_response_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            trace=ExtensionDiagnostics(folder)
            trace.record('command_finished',action='page',args={'endpoint':'event/1/player/7/statistics',
                'token':'secret-token','match_url':'https://www.sofascore.com/event/1?private=secret-query#id:1'},result={'ok':True,'status':403,
                'source_url':'https://www.sofascore.com/event/1?private=secret-query#id:1,tab:lineups',
                'payload':{'secret':'raw-response'},'headers':{'Cookie':'secret-cookie'},
                'diagnostics':{'recovery_stage':'confirmation_read','visible_challenge':False,'player_panels':2,
                    'actions':[{'action':'wait','milliseconds':2714}]}},elapsed=1.25)
            raw=trace.path.read_text(encoding='utf-8');row=json.loads(raw)
            self.assertNotIn('secret',raw);self.assertNotIn('raw-response',raw)
            self.assertEqual(row['result']['source_url'],'https://www.sofascore.com/event/1#id:1')
            self.assertFalse(row['result']['diagnostics']['visible_challenge'])
            self.assertEqual(row['result']['diagnostics']['recovery_stage'],'confirmation_read')
            self.assertEqual(row['result']['status'],403)
            self.assertEqual(row['args']['endpoint'],'event/1/player/7/statistics')
            self.assertEqual(row['args']['match_url'],'https://www.sofascore.com/event/1#id:1')

    def test_concurrent_connection_and_command_events_remain_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            trace=ExtensionDiagnostics(folder)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i:trace.record('command_started',args={'match_id':i}),range(50)))
            rows=[json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual({row['args']['match_id'] for row in rows},set(range(50)))


if __name__=='__main__':unittest.main()

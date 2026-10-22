from tests.paths import system_path
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from collection.reporting import (Reporter, ReportingServer, ReportingStore, capture_logs,
                                  inventory_paths, load_local, read_logs, redact, validate_url)
from collection.ensembleProgress import shared_progress
from storage.pandasStore import atomic_json


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.root = self.base/'worker'; self.ensemble = self.base/'worker-ensemble'
        self.main = self.base/'main'; self.main_ensemble = self.base/'main-ensemble'
        self.token = 'private-worker-token-' + 'x'*32
        atomic_json(system_path(self.root,"identity.json"), {'worker_id':'msi','instance_id':'original-folder'})
        self.options = {'mode':'client','worker_id':'msi','url':'http://127.0.0.1:8766','token':self.token}
        self.runtime = {'format':'soccer-worker-runtime-v1','worker_id':'msi','state':'scraping','updated_at':'2026-10-19T12:00:00+00:00'}
        atomic_json(self.root/'_system/workers/msi.json', self.runtime)
        self.saved = {'format':'soccer-analysis-progress-v1','worker_id':'msi','instance_id':'original-folder','records':{
            'one':{'namespace':'sofascore','competition':'La Liga','season':'2021-2022','match_id':'1',
                   'status':'complete','origins':['msi'],'holders':['msi'],'capabilities':['sofascore_match_stats'],
                   'resources':{'sofascore_stats':'complete'},'resource_origins':{'sofascore_stats':['msi']}}}}
        atomic_json(self.ensemble/'workers/msi.json', self.saved)

    def reporter(self):
        return Reporter(self.root,self.ensemble,'msi',self.options)

    def receiver(self):
        server = ReportingServer(self.main,self.main_ensemble,{'bind':'127.0.0.1','port':0,'tokens':{'msi':self.token}}).start()
        self.addCleanup(server.close)
        self.options['url'] = 'http://127.0.0.1:'+str(server.server.server_port)
        return server

    def test_delta_queue_survives_restart_and_sends_only_changed_records(self):
        first=self.reporter();first.enqueue(reset=True)
        second=self.reporter();self.assertEqual(first.stream,second.stream)
        self.saved['records']['two']={**self.saved['records']['one'],'match_id':'2'}
        atomic_json(self.ensemble/'workers/msi.json',self.saved);second.enqueue()
        del self.saved['records']['one']
        atomic_json(self.ensemble/'workers/msi.json',self.saved);second.enqueue()
        with second.connect() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM queue ORDER BY id')]
        self.assertTrue(rows[0]['reset']);self.assertEqual(set(rows[1]['inventory']['records']),{'two'})
        self.assertEqual(rows[2]['removed'],['one'])
        server=self.receiver();second=self.reporter();self.assertTrue(second.flush())
        saved=json.loads((self.main_ensemble/'reporting/workers/msi.json').read_text())
        self.assertEqual(set(saved['records']),{'two'})
        self.assertEqual(saved['records']['two']['resource_origins'],{'sofascore_stats':['msi']})
        self.assertFalse(second.flush())

    def test_lost_ack_replay_does_not_duplicate_logs(self):
        server=self.receiver();client=self.reporter();client.enqueue(reset=True);client.log('Match 1 saved')
        original=client.request
        def lose(path,payload=None,headers=None):
            result=original(path,payload,headers)
            raise OSError('Response lost')
        with patch.object(client,'request',side_effect=lose):
            with self.assertRaises(OSError):client.flush()
        self.assertEqual(len(read_logs(self.main)['rows']),1)
        restarted=self.reporter();self.assertTrue(restarted.flush())
        self.assertEqual(len(read_logs(self.main)['rows']),1)

    def test_bad_token_and_forged_identity_rejected_without_acknowledging_queue(self):
        self.receiver();client=self.reporter();client.enqueue(reset=True)
        client.options={**self.options,'token':'wrong-'+'z'*32}
        with self.assertRaises(HTTPError) as error:client.flush()
        self.assertEqual(error.exception.code,401)
        client.options=self.options;client.flush()
        other=self.reporter();other.identity['instance_id']='different-data-folder';other.log('Forged')
        with self.assertRaises(HTTPError) as error:other.flush()
        self.assertEqual(error.exception.code,409)
        self.assertEqual(read_logs(self.main)['rows'],[])
        with other.connect() as db:self.assertGreater(db.execute('SELECT COUNT(*) FROM queue').fetchone()[0],0)

    def test_transaction_rolls_back_entire_invalid_batch(self):
        store=ReportingStore(self.main,self.main_ensemble)
        event={'instance_id':'original-folder','stream':'stream','sequence':1,'logs':[{'at':'now','level':'INFO','message':'first'}]}
        bad={**event,'sequence':2,'runtime':{'worker_id':'headless'}}
        with self.assertRaises(ValueError):store.accept('msi',[event,bad])
        self.assertEqual(read_logs(self.main)['rows'],[])
        self.assertEqual(store.inventories({})['inventories'],{})

    def test_existing_git_history_imported_and_new_report_preserves_other_workers(self):
        historical={**self.saved,'worker_id':'headless','instance_id':'other'}
        atomic_json(self.main_ensemble/'workers/headless.json',historical)
        server=self.receiver();client=self.reporter();client.enqueue(reset=True);client.flush();client.pull()
        self.assertTrue((self.ensemble/'reporting/workers/headless.json').exists())
        versions=dict(client.versions);client.pull();self.assertEqual(client.versions,versions)
        self.assertEqual(set(server.store.inventories({})['inventories']),{'msi','headless'})
        self.assertIn('one',shared_progress(self.ensemble/'provenance.json'))
        # Incoming evidence remains remote; it never creates checkpoint/raw files.
        self.assertFalse((self.root/'la-liga').exists())

    def test_store_restart_keeps_inventory_runtime_and_log_tail(self):
        self.receiver();client=self.reporter();client.enqueue(reset=True);client.log('saved');client.flush()
        store=ReportingStore(self.main,self.main_ensemble)
        self.assertIn('msi',store.inventories({})['inventories'])
        runtime=json.loads((self.main_ensemble/'reporting/runtime/msi.json').read_text())
        self.assertIn('received_at',runtime)
        self.assertEqual(read_logs(self.main)['rows'][0]['message'],'saved')

    def test_capture_keeps_console_and_redacts_tokens_urls_and_credential_fields(self):
        client=self.reporter();console=io.StringIO()
        with patch('sys.stdout',console),capture_logs(client):
            print('Sofascore saved match 1')
            print('Authorization: Bearer '+self.token)
            print('cookie=session-private')
            print({"Cookie": "json-cookie-private"})
            print('Bearer standalone-private-token')
            print('GET https://example.test/?secret=abc token=secret-value')
            print('failed match 2')
        self.assertIn(self.token,console.getvalue())
        with client.connect() as db:logs=[json.loads(r[0])['logs'][0] for r in db.execute("SELECT payload FROM queue WHERE kind='logs'")]
        text=json.dumps(logs)
        for secret in (self.token,'session-private','json-cookie-private','standalone-private-token','secret-value','example.test'):self.assertNotIn(secret,text)
        self.assertEqual(logs[-1]['level'],'ERROR')
        self.assertIn('Sofascore saved match 1',text)

    def test_logs_filter_worker_level_text_and_keep_bounded_tail(self):
        store=ReportingStore(self.main,self.main_ensemble)
        with store.connect() as db:
            db.executemany('INSERT INTO logs(worker,at,level,message) VALUES(?,?,?,?)',
                [('msi','now','INFO','old')]*10001)
        event={'instance_id':'original-folder','stream':'stream','sequence':1,
               'logs':[{'at':'now','level':'ERROR','message':'WhoScored failed match 22'}]}
        store.accept('msi',[event])
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],10000)
        rows=read_logs(self.main,'msi','ERROR','whoscored')['rows']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['message'],'WhoScored failed match 22')
        self.assertEqual(read_logs(self.main,'headless')['rows'],[])

    def test_unconfigured_reporter_queues_without_network_and_reuses_queue_when_configured(self):
        options={**self.options,'url':None,'token':''}
        first=Reporter(self.root,self.ensemble,'msi',options).start()
        self.addCleanup(first.close);first.log('offline match saved')
        self.assertIsNone(first.thread);self.assertEqual(first.status['state'],'unconfigured')
        self.receiver();second=self.reporter();second.flush()
        self.assertEqual(read_logs(self.main)['rows'][0]['message'],'offline match saved')

    def test_heartbeat_coalesces_but_saved_match_reports_remain(self):
        client=self.reporter();client.enqueue(reset=True)
        for _ in range(5):client.enqueue(inventory=False)
        client.log('a log');client.enqueue()
        with client.connect() as db:kinds=[r[0] for r in db.execute('SELECT kind FROM queue')]
        self.assertEqual(kinds.count('heartbeat'),1);self.assertEqual(kinds.count('snapshot'),2);self.assertEqual(kinds.count('logs'),1)

    def test_private_address_and_local_configuration_validation(self):
        self.assertEqual(validate_url('http://100.80.12.3:8766'),'http://100.80.12.3:8766')
        self.assertEqual(validate_url('http://100.106.146.82'),'http://100.106.146.82:8766')
        for url in ('http://public.example','http://8.8.8.8','http://user:secret@127.0.0.1:8766','http://127.0.0.1:8766/path','http://127.0.0.1:bad'):
            with self.subTest(url=url),self.assertRaises(ValueError):validate_url(url)
        atomic_json(system_path(self.root,"reporting.json"),{'mode':'server','bind':'0.0.0.0','port':8766,'tokens':{'msi':self.token}})
        with self.assertRaises(ValueError):load_local(self.root)

    def test_local_inventory_wins_over_remote_copy_for_own_holder(self):
        atomic_json(self.ensemble/'reporting/workers/msi.json',{**self.saved,'records':{}})
        self.assertEqual(inventory_paths(self.ensemble,local_worker='msi'),[self.ensemble/'workers/msi.json'])
        self.assertEqual(inventory_paths(self.ensemble),[self.ensemble/'reporting/workers/msi.json'])

    def test_extension_operational_diagnostics_reach_main_without_response_bodies(self):
        from providers.transports.diagnostics import ExtensionDiagnostics
        self.receiver();client=self.reporter()
        diagnostics=ExtensionDiagnostics(self.root/'extension_diagnostics/whoscored')
        with capture_logs(client):
            diagnostics.record('response',action='open_match',args={'match_id':'22'},
                result={'ok':False,'error':'Verification required','payload':{'cookie':'private-cookie'},'token':'secret-token'})
        client.flush();rows=read_logs(self.main)['rows']
        self.assertEqual(rows[0]['level'],'WARNING')
        self.assertIn('Extension whoscored',rows[0]['message']);self.assertIn('Verification required',rows[0]['message'])
        self.assertNotIn('private-cookie',rows[0]['message']);self.assertNotIn('secret-token',rows[0]['message'])

    def test_background_network_does_not_block_queuing_saved_match(self):
        client=self.reporter();entered=threading.Event();release=threading.Event()
        def blocked(*args,**kwargs):entered.set();release.wait(3);return {'inventories':{},'versions':{}}
        with patch.object(client,'request',side_effect=blocked):
            client.start()
            try:
                self.assertTrue(entered.wait(1))
                # The network thread is still held; enqueue must use only local storage.
                client.enqueue();client.log('next saved match')
                with client.connect() as db:self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM queue').fetchone()[0],3)
            finally:release.set();client.close()

    def test_slow_inventory_download_cannot_block_progress_upload(self):
        self.receiver();client=self.reporter();entered=threading.Event();release=threading.Event();delivered=threading.Event()
        original=client.request
        def request(path,payload=None,headers=None):
            if path=='/v1/inventories':
                entered.set();release.wait(4);return {'inventories':{},'versions':{}}
            value=original(path,payload,headers);return value
        original_flush=client.flush
        def flushed():
            result=original_flush()
            if result:delivered.set()
            return result
        with patch.object(client,'request',side_effect=request),patch.object(client,'flush',side_effect=flushed):
            client.start()
            try:
                self.assertTrue(entered.wait(1));self.assertTrue(delivered.wait(2),'Upload waited for the blocked inventory download')
                self.assertEqual(client.status['state'],'connected')
            finally:release.set();client.close()

    def test_failed_inventory_download_does_not_stop_uploads(self):
        self.receiver();client=self.reporter();delivered=threading.Event();original=client.request
        def request(path,payload=None,headers=None):
            if path=='/v1/inventories':raise TimeoutError('slow download')
            value=original(path,payload,headers);return value
        original_flush=client.flush
        def flushed():
            result=original_flush()
            if result:delivered.set()
            return result
        with patch.object(client,'request',side_effect=request),patch.object(client,'flush',side_effect=flushed):
            client.start()
            try:self.assertTrue(delivered.wait(2));self.assertEqual(client.status['state'],'connected')
            finally:client.close()

    def test_diagnostics_print_no_token_and_identify_wrong_identity(self):
        from collection.reporting import diagnose
        self.receiver();client=self.reporter();client.enqueue(reset=True);client.flush()
        atomic_json(system_path(self.root,"reporting.json"),self.options)
        output=io.StringIO()
        with patch('sys.stdout',output):self.assertEqual(diagnose(self.root),0)
        self.assertIn('Connection OK',output.getvalue());self.assertNotIn(self.token,output.getvalue())
        atomic_json(system_path(self.root,"identity.json"),{'worker_id':'msi','instance_id':'different-folder'})
        output=io.StringIO()
        with patch('sys.stdout',output):self.assertEqual(diagnose(self.root),2)
        self.assertIn('IDENTITY CONFLICT',output.getvalue())
        self.assertNotIn(self.token,output.getvalue())

    def test_large_inventory_response_is_compressed_and_client_decodes_it(self):
        from urllib.request import Request
        historical={**self.saved,'worker_id':'headless','instance_id':'other',
            'records':{str(i):{**self.saved['records']['one'],'match_id':str(i)} for i in range(40)}}
        atomic_json(self.main_ensemble/'workers/headless.json',historical)
        self.receiver();client=self.reporter()
        request=Request(self.options['url']+'/v1/inventories',headers={'Authorization':'Bearer '+self.token,'Accept-Encoding':'gzip'})
        with client.opener.open(request,timeout=5) as response:
            self.assertEqual(response.headers.get('Content-Encoding'),'gzip');response.read()
        client.pull()
        received=json.loads((self.ensemble/'reporting/workers/headless.json').read_text())
        self.assertEqual(len(received['records']),40)

    def test_important_and_season_completion_log_filters(self):
        from collection.reporting import console_level
        self.receiver();client=self.reporter()
        client.log('Routine extension navigation')
        client.log('[Season started] Premier League 2022-2023')
        client.log('[Season completed] Premier League 2022-2023')
        client.log('Provider failed','ERROR');client.log('Verification paused','WARNING');client.flush()
        self.assertEqual(len(read_logs(self.main,focus='important')['rows']),4)
        self.assertEqual(len(read_logs(self.main,focus='seasons')['rows']),1)
        self.assertEqual(len(read_logs(self.main,focus='errors')['rows']),1)
        self.assertEqual(len(read_logs(self.main,focus='milestones')['rows']),2)
        self.assertEqual(console_level('0 failed / partial'),'INFO')

    def test_log_catchup_does_not_decode_or_serialize_inventory_for_each_event(self):
        from collection.reporting import encode
        store=ReportingStore(self.main,self.main_ensemble)
        client=self.reporter();client.enqueue(reset=True)
        with client.connect() as db:
            seq,payload=db.execute('SELECT id,payload FROM queue ORDER BY id LIMIT 1').fetchone()
        base={'instance_id':'original-folder','stream':client.stream}
        store.accept('msi',[{**json.loads(payload),**base,'sequence':seq}])
        original=json.loads((self.main_ensemble/'reporting/workers/msi.json').read_text())
        events=[{**base,'sequence':seq+i+1,'logs':[{'at':'now','level':'INFO','message':str(i)}]} for i in range(50)]
        with patch('collection.reporting.encode',wraps=encode) as serializer:
            store.accept('msi',events)
        inventories=[args[0] for args,kwargs in serializer.call_args_list
                     if args and isinstance(args[0],dict) and args[0].get('format')=='soccer-analysis-progress-v1']
        self.assertEqual(inventories,[])
        self.assertEqual(len(read_logs(self.main)['rows']),50)
        self.assertEqual(json.loads((self.main_ensemble/'reporting/workers/msi.json').read_text()),original)

    def test_repeated_identity_batches_merge_once_and_repair_after_lost_ack(self):
        from providers.identityReview import validate
        store=ReportingStore(self.main,self.main_ensemble)
        proposal=validate({'kind':'player','provider':'fotmob','source_id':'22','target_id':'11',
                          'source_name':'Player','target_name':'Player','score':.9,'contexts':[]})
        events=[{'instance_id':'original-folder','stream':'original-stream','sequence':i+1,
                 'identity_suggestions':[{**proposal,'contexts':[{'match':str(i)}]}]} for i in range(50)]
        ingest=store.identities.ingest
        with patch.object(store.identities,'ingest',wraps=ingest) as process:
            store.accept('msi',events)
        self.assertEqual(process.call_count,1)
        self.assertEqual(len(process.call_args.args[0]),1)
        self.assertEqual(len(process.call_args.args[0][0]['contexts']),5)
        # If the identity export failed after durable receipt, replay must repair it.
        with store.identities.connect() as db:db.execute('DELETE FROM proposals')
        store.accept('msi',events)
        self.assertEqual(len(store.identities.proposals()),1)

    def test_identity_observation_timestamps_do_not_republish_unchanged_evidence(self):
        from providers.identityReview import ReviewStore
        proposal={'kind':'player','provider':'fotmob','source_id':'22','target_id':'11',
                  'source_name':'Player','target_name':'Player','score':.9,'contexts':[]}
        review=ReviewStore(self.root);review.ingest([proposal]);client=self.reporter();client.enqueue()
        review.ingest([proposal]);client.enqueue()
        with client.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queue WHERE kind='identities'").fetchone()[0],1)
        review.ingest([{**proposal,'score':.95}]);client.enqueue()
        with client.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queue WHERE kind='identities'").fetchone()[0],2)

    def test_timeout_message_distinguishes_delayed_acknowledgement_from_authentication(self):
        from collection.reporting import connection_error
        from urllib.error import URLError
        self.assertIn('acknowledge',connection_error(TimeoutError()))
        self.assertIn('acknowledge',connection_error(URLError(TimeoutError())))
        self.assertIn('token',connection_error(HTTPError('http://localhost',401,'Unauthorized',{},None)))

    def test_duplicate_identity_evidence_is_validated_once_despite_observation_timestamps(self):
        from providers.identityReview import validate
        store=ReportingStore(self.main,self.main_ensemble)
        proposal={'kind':'player','provider':'fotmob','source_id':'22','target_id':'11',
                  'source_name':'Player','target_name':'Player','score':.9,'contexts':[]}
        events=[{'instance_id':'original-folder','stream':'stream','sequence':i+1,
                 'identity_suggestions':[{**proposal,'last_seen':str(i)}]} for i in range(50)]
        with patch('providers.identityReview.validate',wraps=validate) as validator,patch.object(store.identities,'ingest'):
            store.accept('msi',events)
        self.assertEqual(validator.call_count,1)

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
        atomic_json(self.root/'identity.json', {'worker_id':'msi','instance_id':'original-folder'})
        self.options = {'mode':'client','worker_id':'msi','url':'http://127.0.0.1:8766','token':self.token}
        self.runtime = {'format':'soccer-worker-runtime-v1','worker_id':'msi','state':'scraping','updated_at':'2026-10-19T12:00:00+00:00'}
        atomic_json(self.root/'worker_runtime/msi.json', self.runtime)
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
        for url in ('http://public.example','http://8.8.8.8','http://user:secret@127.0.0.1:8766','http://127.0.0.1:8766/path','http://127.0.0.1:bad'):
            with self.subTest(url=url),self.assertRaises(ValueError):validate_url(url)
        atomic_json(self.root/'reporting.json',{'mode':'server','bind':'0.0.0.0','port':8766,'tokens':{'msi':self.token}})
        with self.assertRaises(ValueError):load_local(self.root)

    def test_local_inventory_wins_over_remote_copy_for_own_holder(self):
        atomic_json(self.ensemble/'reporting/workers/msi.json',{**self.saved,'records':{}})
        self.assertEqual(inventory_paths(self.ensemble,local_worker='msi'),[self.ensemble/'workers/msi.json'])
        self.assertEqual(inventory_paths(self.ensemble),[self.ensemble/'reporting/workers/msi.json'])

    def test_extension_operational_diagnostics_reach_main_without_response_bodies(self):
        from providers.extensionDiagnostics import ExtensionDiagnostics
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

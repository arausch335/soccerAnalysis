from http.client import RemoteDisconnected
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.request import Request,urlopen

from collection.reporting import ReportingServer,Reporter,read_logs
from collection.storageServer import StorageServer
from dashboard.server import start_server
from providers.transports.bridge import ExtensionBridge
from storage.pandasStore import atomic_json


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.token='x'*40

    def check_disconnected_response(self,server,request):
        handler=server.RequestHandlerClass
        for error in (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            with self.subTest(error=error.__name__),patch.object(handler,'end_headers',side_effect=error()),patch.object(handler,'send_error') as second_response,patch.object(server,'handle_error') as traceback:
                with self.assertRaises(RemoteDisconnected):urlopen(request,timeout=3)
                second_response.assert_not_called();traceback.assert_not_called()
        with urlopen(request,timeout=3) as response:self.assertEqual(response.status,200)

    def test_dashboard_disconnected_headers_do_not_send_second_error_or_stop_server(self):
        model=Mock();model.status.return_value={'ok':True}
        with patch('dashboard.server.DashboardModel',return_value=model):server=start_server(self.root,self.root/'ensemble',None,'main',0)
        self.addCleanup(server.close)
        self.check_disconnected_response(server.server,Request(server.url+'/api/status'))

    def test_reporting_survives_disconnection_and_replay_does_not_duplicate_logs(self):
        server=ReportingServer(self.root/'main',self.root/'ensemble',{'bind':'127.0.0.1','port':0,'tokens':{'msi':self.token}}).start();self.addCleanup(server.close)
        url=f'http://127.0.0.1:{server.server.server_port}'
        self.check_disconnected_response(server.server,Request(url+'/v1/health',headers={'Authorization':'Bearer '+self.token}))
        worker=self.root/'msi';atomic_json(worker/'_system/config/identity.json',{'worker_id':'msi','instance_id':'original'})
        reporter=Reporter(worker,self.root/'msi-ensemble','msi',{'worker_id':'msi','url':url,'token':self.token})
        reporter.log('Saved match remains durable')
        with patch.object(server.server.RequestHandlerClass,'end_headers',side_effect=BrokenPipeError()):
            with self.assertRaises(RemoteDisconnected):reporter.flush()
        with reporter.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM queue').fetchone()[0],1)
        self.assertTrue(reporter.flush());self.assertEqual(len(read_logs(self.root/'main')['rows']),1)

    def test_storage_disconnect_is_not_misreported_as_storage_failure(self):
        server=StorageServer(self.root,{'url':'http://127.0.0.1:0','tokens':{'main':{'role':'consumer','token':self.token}}},reserve=0).start();self.addCleanup(server.close)
        self.check_disconnected_response(server.server,Request(server.url+'/v1/status',headers={'Authorization':'Bearer '+self.token}))
        with patch.object(server.archive,'status',side_effect=OSError('Real storage error')):
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as failure:urlopen(Request(server.url+'/v1/status',headers={'Authorization':'Bearer '+self.token}),timeout=3)
        self.assertEqual(failure.exception.code,503)

    def test_extension_connect_disconnect_during_headers_is_quiet_and_can_retry(self):
        bridge=ExtensionBridge(report=lambda *a,**k:None);bridge.start();self.addCleanup(bridge.close)
        request=Request('http://'+bridge.address+'/v1/connect',data=json.dumps({'client_id':'a'*20}).encode(),headers={'Authorization':'Bearer '+bridge.token,'Content-Type':'application/json'})
        self.check_disconnected_response(bridge.server,request)
        self.assertTrue(bridge.ready)

    def test_interrupted_dataset_stream_releases_reader_and_can_be_downloaded_again(self):
        from http.client import IncompleteRead
        from collection.storageTransfer import FORMAT,digest
        server=StorageServer(self.root,{'url':'http://127.0.0.1:0','tokens':{'main':{'role':'consumer','token':self.token}}},reserve=0).start();self.addCleanup(server.close)
        source=self.root/'player_stats.csv';source.write_text('id,value\n1,2\n')
        key='premier-league/2021-2022/player_stats.csv';sha=digest(source)
        manifest={'format':FORMAT,'kind':'dataset','worker':'msi','files':[{'path':key,'sha256':sha,'size':source.stat().st_size}]}
        server.archive.publish(manifest,{key:source})
        request=Request(server.url+'/v1/dataset/blobs/'+sha,headers={'Authorization':'Bearer '+self.token})
        with patch('collection.storageServer.shutil.copyfileobj',side_effect=BrokenPipeError()),patch.object(server.server.RequestHandlerClass,'reply') as error_response,patch.object(server.server,'handle_error') as traceback:
            with urlopen(request,timeout=3) as response:
                with self.assertRaises(IncompleteRead):response.read()
            error_response.assert_not_called();traceback.assert_not_called()
        self.assertEqual(server.archive.readers,0)
        with urlopen(request,timeout=3) as response:self.assertEqual(response.read(),source.read_bytes())

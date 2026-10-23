from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import time
import unittest

from providers.transports.bridge import ExtensionBridge
from providers.httpClient import ProviderRequestError


def quiet(*args,**kwargs):pass


class ExtensionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge=ExtensionBridge(report=quiet,connect_timeout=.2)
        self.bridge.start()
        self.cid='a'*32

    def tearDown(self):self.bridge.close()

    def http(self,path,value=None,*,token=True,origin=None,host=None,client=None):
        connection=http.client.HTTPConnection(self.bridge.address,timeout=3)
        headers={'X-Collector-Client':client or self.cid}
        if token:headers['Authorization']='Bearer '+self.bridge.token
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        data=None if value is None else json.dumps(value)
        connection.request('GET' if value is None else 'POST',path,body=data,headers=headers)
        response=connection.getresponse();result=json.loads(response.read())
        connection.close()
        return response.status,result

    def connect(self):
        self.assertEqual(self.http('/v1/connect',{'client_id':self.cid})[0],200)

    def test_profile_capabilities_are_negotiated_and_legacy_clients_remain_compatible(self):
        self.connect();self.assertFalse(self.bridge.capabilities)
        self.assertEqual(self.http('/v1/connect',{'client_id':self.cid,'capabilities':['player-profiles-v1']})[0],200)
        self.assertIn('player-profiles-v1',self.bridge.capabilities)
        self.assertEqual(self.http('/v1/connect',{'client_id':self.cid,'capabilities':'invalid'})[0],400)
        self.assertIn('player-profiles-v1',self.bridge.capabilities)

    def test_late_initial_connection_does_not_poison_future_requests(self):
        self.bridge.connect_timeout=.01
        with self.assertRaisesRegex(ProviderRequestError,'not connected yet'):self.bridge.request('page',{})
        self.assertFalse(self.bridge.unavailable);self.assertFalse(self.bridge.ready)
        self.connect();self.assertTrue(self.bridge.ready)

    def test_auth_origin_and_host_boundaries(self):
        for options,status in (({'token':False},401),({'origin':'https://evil.test'},403),
                               ({'host':'evil.test'},403)):
            with self.subTest(options=options):
                self.assertEqual(self.http('/v1/connect',{'client_id':self.cid},**options)[0],status)
        self.assertEqual(self.http('/v1/connect',{'client_id':self.cid},origin='chrome-extension://'+'a'*32)[0],200)
        self.assertEqual(self.http('/v1/unknown')[0],404)
        self.assertEqual(self.bridge.server.server_address[0],'127.0.0.1')

    def test_one_connected_tab_and_result_identity(self):
        self.connect()
        self.assertEqual(self.http('/v1/connect',{'client_id':'b'*32})[0],409)
        self.assertEqual(self.http('/v1/command',client='b'*32)[0],409)
        with ThreadPoolExecutor() as pool:
            future=pool.submit(self.bridge.request,'page',{'operation':'read'},timeout=2)
            status,body=self.http('/v1/command')
            self.assertEqual(status,200)
            command=body['command']
            self.assertEqual(command['action'],'page')
            self.assertNotIn(self.bridge.token,json.dumps(body))
            self.assertEqual(self.http('/v1/result',{'id':'wrong','result':{'ok':True}})[0],409)
            self.assertEqual(self.http('/v1/result',{'id':command['id'],'result':{'ok':True,'status':200,'payload':{'match':1}}})[0],200)
            self.assertEqual(future.result()['payload'],{'match':1})

    def test_verification_progress_is_authenticated_filtered_and_does_not_complete_command(self):
        from unittest.mock import patch
        self.connect()
        self.assertEqual(self.http('/v1/diagnostic', {'id': 'expired', 'result': {}})[0], 409)
        with ThreadPoolExecutor() as pool, patch.object(self.bridge, 'record') as record:
            future=pool.submit(self.bridge.request,'verify',{},timeout=.01)
            _,body=self.http('/v1/command')
            command=body['command']
            value={'id':command['id'],'result':{'recovery_stage':'confirmation_read','status':403}}
            self.assertEqual(self.http('/v1/diagnostic',value,token=False)[0],401)
            self.assertEqual(self.http('/v1/diagnostic',value,client='b'*32)[0],409)
            self.assertEqual(self.http('/v1/diagnostic',value)[0],200)
            record.assert_any_call('verification_progress',action='verify',result=value['result'])
            self.assertFalse(future.done())
            self.http('/v1/result',{'id':command['id'],'result':{'ok':True}})
            self.assertTrue(future.result(timeout=1)['ok'])

    def test_pause_freezes_command_deadline_until_resume(self):
        self.connect();self.http('/v1/pause',{})
        with ThreadPoolExecutor() as pool:
            future=pool.submit(self.bridge.request,'page',{},timeout=.05)
            _,body=self.http('/v1/command')
            time.sleep(.1)
            self.assertFalse(future.done())
            self.http('/v1/resume',{})
            self.http('/v1/result',{'id':body['command']['id'],'result':{'ok':True}})
            self.assertTrue(future.result()['ok'])
            self.assertGreaterEqual(self.bridge.waiting_seconds,.09)

    def test_disconnect_unblocks_pending_request_and_can_reconnect(self):
        self.connect()
        with ThreadPoolExecutor() as pool:
            future=pool.submit(self.bridge.request,'page',{},timeout=2)
            self.http('/v1/command')
            self.http('/v1/disconnect',{})
            with self.assertRaisesRegex(ProviderRequestError,'disconnected'):future.result()
        self.assertFalse(self.bridge.connected.is_set())
        self.connect()

    def test_verification_does_not_expire_while_browser_clears_it(self):
        self.connect()
        with ThreadPoolExecutor() as pool:
            future=pool.submit(self.bridge.request,'verify',{},timeout=.01)
            _,body=self.http('/v1/command')
            time.sleep(.05)
            self.assertFalse(future.done())
            self.http('/v1/result',{'id':body['command']['id'],'result':{'ok':True}})
            self.assertTrue(future.result(timeout=1)['ok'])
            self.assertGreaterEqual(self.bridge.waiting_seconds,.04)
        self.assertIsNone(self.bridge.pending)
        self.assertFalse(self.bridge.unavailable)
        self.assertEqual(self.http('/v1/result',{'id':'expired','result':{'ok':True}})[0],409)


if __name__=='__main__':unittest.main()

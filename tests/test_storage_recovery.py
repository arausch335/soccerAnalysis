"""Explicit raw removal and invalid builds cannot block unrelated imports."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from collection.removedCollections import apply_removals
from collection.storageServer import Archive,StorageServer
from collection.storageAgent import StorageAgent
from collection.storageTransfer import Outbox,Peer
from collection.backgroundImports import import_parts
from storage.pandasStore import atomic_json
from tests.test_ensemble import populate

REMOVED='bundesliga/2018-2019'
GOOD='premier-league/2024-2025'


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.output=self.base/'dataset'
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})

    def test_removal_is_exact_recoverable_and_keeps_fixture_history(self):
        stub=self.root/REMOVED/'whoscored_job.json';atomic_json(stub,{'competition':'Bundesliga','season':'2018/2019'})
        old=stub.read_bytes();other=self.root/'bundesliga/2019-2020/whoscored_job.json';atomic_json(other,{})
        fixture=self.root/'_system/fixtures/cross_provider/sofascore/bundesliga/2018-2019.json';atomic_json(fixture,{'events':[1]})
        archive=Archive(self.root)
        with archive.connect() as db:db.execute("INSERT INTO builds VALUES(?,'pending',1)",(REMOVED,));db.commit()
        self.assertEqual(apply_removals(self.root,dataset=self.output,archive=archive,report=lambda _:None),[REMOVED])
        self.assertFalse(stub.exists());self.assertTrue(other.exists());self.assertTrue(fixture.exists())
        copies=list((self.root/'_system/removed_raw_collections'/REMOVED).rglob('whoscored_job.json'))
        self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_bytes(),old)
        self.assertEqual(archive.builds(),[]);self.assertEqual(archive.status()['removed_builds'],1)
        self.assertEqual(apply_removals(self.root,archive=archive,report=lambda _:None),[])
        archive.schedule_build(REMOVED);self.assertEqual(archive.builds(),[])

    def test_old_transfer_cannot_restore_removed_raw_collection(self):
        source=self.base/'old';atomic_json(source/'.soccer-scraper.json',{'format':1})
        atomic_json(source/REMOVED/'whoscored_job.json',{})
        files={'.soccer-scraper.json':source/'.soccer-scraper.json',REMOVED+'/whoscored_job.json':source/REMOVED/'whoscored_job.json'}
        self.assertFalse(import_parts(self.root,files,owner='main',batch='old',report=lambda *a,**k:None))
        self.assertFalse((self.root/REMOVED).exists())

    def test_removed_upload_acknowledged_without_transferring_bytes_or_requeueing(self):
        server=StorageServer(self.root,{'url':'http://127.0.0.1:0','tokens':{'headless':{'role':'producer','token':'p'*40}}},reserve=0).start()
        self.addCleanup(server.close)
        sender=self.base/'headless';atomic_json(sender/'.soccer-scraper.json',{'format':1});atomic_json(sender/REMOVED/'whoscored_job.json',{})
        box=Outbox(sender,'headless');files={'.soccer-scraper.json':sender/'.soccer-scraper.json',REMOVED+'/whoscored_job.json':sender/REMOVED/'whoscored_job.json'}
        mid=box.enqueue(files,kind='raw',season=REMOVED,completed=True)
        self.assertTrue(box.send_one(Peer(server.url,'p'*40)));self.assertEqual(box.queued(),0)
        self.assertEqual(server.archive.upload('headless',mid)[1],'removed')
        self.assertFalse(list(server.archive.objects.iterdir()));self.assertFalse(server.archive.completed())

    def test_metadata_only_build_is_set_aside_before_snapshot_and_next_job_publishes(self):
        populate(self.root,limit=1)
        stub=self.root/'bundesliga/2019-2020/whoscored_job.json';atomic_json(stub,{})
        archive=Archive(self.root);archive.schedule_build('bundesliga/2019-2020');archive.schedule_build(GOOD)
        agent=StorageAgent(self.root,'msi',{'hub':'msi','consumers':['main']},dataset_root=self.output,log=lambda _:None)
        class Server:pass
        agent.server=Server();agent.server.archive=archive
        calls=[]
        def build(root,output,names):
            calls.extend(names);(output/'match_ids.csv').write_text('sofascore_id\n1\n');return 0
        with patch('dataset_builder.build_dataset',side_effect=build),patch('collection.backgroundImports.snapshot_data',wraps=__import__('collection.backgroundImports',fromlist=['snapshot_data']).snapshot_data) as snapshot:
            agent.maintenance_once()
        self.assertEqual(calls,['premier-league-2024-25']);self.assertEqual(snapshot.call_count,1)
        self.assertIsNotNone(archive.publication());self.assertEqual(archive.builds(),[])
        self.assertEqual(archive.status()['blocked_builds'],1)
        blocked=next(r for r in archive.diagnostics()['builds'] if r['state']=='blocked')
        self.assertIn('No saved combined job',blocked['error'])

    def test_diagnostic_api_exposes_metadata_to_consumer_and_rejects_producer(self):
        options={'url':'http://127.0.0.1:0','tokens':{'main':{'role':'consumer','token':'c'*40},'headless':{'role':'producer','token':'p'*40}}}
        server=StorageServer(self.root,options,reserve=0).start();self.addCleanup(server.close)
        server.archive.schedule_build(GOOD);_,revision=server.archive.build_jobs()[0]
        server.archive.block_build(GOOD,revision,ValueError('Bad input'))
        value=Peer(server.url,'c'*40).request('/v1/diagnostics')
        self.assertEqual(value['builds'][0]['state'],'blocked');self.assertNotIn('files',value)
        with self.assertRaises(HTTPError):Peer(server.url,'p'*40).request('/v1/diagnostics')
        server.archive.schedule_build(GOOD);self.assertEqual(len(server.archive.build_jobs()),1)

    def test_quarantine_diagnostic_contains_identity_fields_without_raw_events(self):
        from collection.storageTransfer import digest
        from collection.storageAgent import season_files
        from storage.pandasStore import PandasStore
        from providers.cache import ResponseCache
        populate(self.root,limit=1)
        source=self.root/GOOD;store=PandasStore(source/'match_checkpoints/1');data=store.from_csv()
        data.matches[1].metadata['provider_match_ids']={'whoscored':11};store.to_csv(data)
        raw=ResponseCache(source/'raw').path('whoscored','match/11')
        atomic_json(raw,{'provider':'whoscored','key':'match/11','payload':{'match_id':11,'matchCentreData':{
            'home':{'teamId':900,'name':'Wrong home'},'away':{'teamId':901,'name':'Away'},
            'startDate':'2024-08-10','events':[{'secret_raw_payload':'must stay on MSI'}]}}})
        archive=Archive(self.root);files=season_files(self.root,GOOD);entries=[]
        for name,path in files.items():
            sha=digest(path);(archive.objects/sha).write_bytes(path.read_bytes());entries.append({'path':name,'sha256':sha})
        value=archive.quarantine_identity({'files':entries},raw.stem)
        self.assertEqual(value['whoscored_id'],'11');self.assertEqual(value['teams']['home']['id'],900)
        self.assertEqual(value['checkpoints'][0]['match_id'],'1')
        self.assertNotIn('events',json.dumps(value));self.assertNotIn('secret_raw_payload',json.dumps(value))

    def test_idle_producer_can_drain_saved_queue_without_starting_scraping(self):
        from tools.storage_diagnostics import drain,queue_status
        from collection.storageConfig import path_for
        options={'url':'http://127.0.0.1:0','tokens':{'headless':{'role':'producer','token':'p'*40}}}
        server=StorageServer(self.base/'hub',options,reserve=0).start();self.addCleanup(server.close)
        atomic_json(self.root/GOOD/'job.json',{'saved':True})
        outbox=Outbox(self.root,'headless');outbox.enqueue({'.soccer-scraper.json':self.root/'.soccer-scraper.json',
            GOOD+'/job.json':self.root/GOOD/'job.json'},kind='raw',season=GOOD)
        atomic_json(path_for(self.root),{'worker_id':'headless','role':'producer','hub':'msi','url':server.url,'token':'p'*40})
        self.assertEqual(queue_status(self.root)['queued_uploads'],1)
        self.assertTrue(drain(self.root,self.output,'headless',{'hub':'msi','url':server.url},report=lambda *a,**k:None))
        self.assertEqual(queue_status(self.root)['queued_uploads'],0)
        self.assertEqual(server.archive.status()['received'],1)


if __name__=='__main__':unittest.main()

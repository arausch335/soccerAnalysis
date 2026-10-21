"""Real loopback protocol tests; no provider network or browser required."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from collection.storageConfig import Provisioner,install,path_for
from collection.storageTransfer import Outbox,Peer,FORMAT,CHUNK,digest,identifier,validate_manifest,safe_path
from collection.storageServer import Archive,StorageServer
from collection.storageAgent import StorageAgent,season_files,install_dataset
from storage.pandasStore import atomic_json,PandasStore


def quiet(*args,**kwargs):pass


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        self.root=self.base/'headless';atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        self.season='premier-league/2024-2025'
        self.raw=self.root/self.season/'raw/whoscored/match.json';atomic_json(self.raw,{'events':[1,2,3]})
        self.files={'.soccer-scraper.json':self.root/'.soccer-scraper.json',self.raw.relative_to(self.root).as_posix():self.raw}
        self.options={'url':'http://127.0.0.1:0','tokens':{'headless':{'role':'producer','token':'p'*40},'main':{'role':'consumer','token':'c'*40}}}
        self.server=StorageServer(self.base/'msi',self.options,reserve=0).start();self.addCleanup(self.server.close)
        self.peer=Peer(self.server.url,'p'*40);self.reader=Peer(self.server.url,'c'*40)
        self.outbox=Outbox(self.root,'headless')

    def queue(self,completed=False):return self.outbox.enqueue(self.files,kind='raw',season=self.season,completed=completed)

    def test_acknowledged_files_remain_local_and_duplicates_do_not_requeue(self):
        mid=self.queue();self.assertTrue(self.outbox.send_one(self.peer))
        self.assertEqual(self.outbox.queued(),0);self.assertTrue(self.raw.is_file())
        self.assertFalse((self.outbox.directory/mid).exists())
        self.assertEqual(self.queue(),mid);self.assertEqual(self.outbox.queued(),0)
        self.assertEqual(self.server.archive.status()['received'],1)
        self.assertEqual(self.server.archive.completed(),[])

    def test_resume_chunks_after_interruption_and_reject_corruption(self):
        self.raw.write_bytes(b'x'*(CHUNK+71));mid=self.queue()
        manifest=json.loads((self.outbox.directory/mid/'manifest.json').read_text())
        self.peer.request('/v1/uploads',manifest)
        item=next(f for f in manifest['files'] if f['size']>CHUNK);route='/v1/uploads/'+mid+'/blobs/'+item['sha256']
        self.peer.request(route+'?offset=0',method='PUT',data=b'x'*CHUNK)
        self.assertEqual(self.peer.request(route)['offset'],CHUNK)
        with self.assertRaises(HTTPError):self.peer.request(route+'?offset=1',method='PUT',data=b'x')
        with self.assertRaises(HTTPError):self.peer.request(route+'?offset='+str(CHUNK),method='PUT',data=b'z'*71)
        self.assertEqual(self.peer.request(route)['offset'],0)
        self.assertTrue(self.outbox.send_one(self.peer))
        self.assertEqual((self.server.archive.objects/item['sha256']).read_bytes(),self.raw.read_bytes())

    def test_offline_outbox_survives_restart_and_zero_byte_files(self):
        self.raw.write_bytes(b'');mid=self.queue(True)
        with self.assertRaises(OSError):self.outbox.send_one(Peer('http://127.0.0.1:1','p'*40))
        resumed=Outbox(self.root,'headless');self.assertEqual(resumed.queued(),1)
        self.assertTrue(resumed.send_one(self.peer));self.assertEqual(self.server.archive.completed()[0][0],mid)

    def test_consumers_cannot_upload_or_read_raw_objects(self):
        mid=self.queue();self.outbox.send_one(self.peer)
        raw_sha=digest(self.raw)
        for path,value in [('/v1/uploads',{}),('/v1/dataset/blobs/'+raw_sha,None),('/v1/uploads/'+mid+'/blobs/'+raw_sha,None)]:
            with self.subTest(path=path),self.assertRaises(HTTPError) as caught:self.reader.request(path,value)
            self.assertEqual(caught.exception.code,403)
        with self.assertRaises(HTTPError):Peer(self.server.url,'bad').request('/v1/dataset')
        with self.assertRaises(HTTPError):self.peer.request('/v1/dataset')

    def test_manifest_paths_and_raw_dataset_mix_are_rejected(self):
        for path in ('../x.json','/tmp/x.json','premier-league/2024-2025/../../x.json',r'C:\x.json',
                     'premier-league/2024-2025/_system/token.json','premier-league/2024-2025/raw/test.json',
                     'premier-league/2024-2025/match_checkpoints/1/CURRENT.json'):
            with self.subTest(path=path),self.assertRaises(ValueError):safe_path(path,'dataset')
        mid=self.queue();manifest=json.loads((self.outbox.directory/mid/'manifest.json').read_text())
        manifest['worker']='main'
        with self.assertRaises(HTTPError) as caught:self.peer.request('/v1/uploads',manifest)
        self.assertEqual(caught.exception.code,403)

    def test_dataset_only_publication_is_verified_and_preserves_other_seasons(self):
        dataset=self.base/'dataset';csv=dataset/self.season/'player_stats.csv';csv.parent.mkdir(parents=True);csv.write_text('id,value\n1,2\n')
        ids=dataset/'concordance/player_ids.csv';ids.parent.mkdir();ids.write_text('sofascore,reep\n1,2\n')
        files={p.relative_to(dataset).as_posix():p for p in (csv,ids)}
        manifest={'format':FORMAT,'kind':'dataset','worker':'msi','files':[{'path':p,'sha256':digest(f),'size':f.stat().st_size} for p,f in files.items()]}
        self.server.archive.publish(manifest,files)
        self.assertEqual(self.reader.request('/v1/dataset')['manifest'],manifest)
        output=self.base/'main-dataset';other=output/'la-liga/2023-2024/player_stats.csv';other.parent.mkdir(parents=True);other.write_text('kept')
        install_dataset(self.reader,manifest,output,self.base/'downloads')
        self.assertEqual((output/self.season/'player_stats.csv').read_text(),csv.read_text());self.assertEqual(other.read_text(),'kept')
        self.assertFalse(list(output.rglob('raw')))
        # A bad publication never replaces valid local files.
        manifest['files'][0]['size']+=1
        with self.assertRaises(ValueError):install_dataset(self.reader,manifest,output,self.base/'downloads')
        self.assertEqual((output/self.season/'player_stats.csv').read_text(),csv.read_text())

    def test_real_season_build_reaches_main_with_concordance_without_raw(self):
        from collection.seasonRunner import SeasonRunner
        from collection.workerIdentity import ensure_identity
        from tests.test_providers import Sofa
        hubroot=self.base/'msi';directory=hubroot/self.season
        atomic_json(hubroot/'.soccer-scraper.json',{'format':1});ensure_identity(hubroot,'msi')
        atomic_json(directory/'job.json',{'competition':'Premier League','season':'2024/25','providers':['sofascore'],'spatial':[]})
        SeasonRunner({'sofascore':Sofa()},directory,providers=['sofascore'],spatial=(),report=quiet).run('EPL','2024',limit=1,build_csvs=False)
        settings={'hub':'msi','consumers':['main'],'url':self.server.url}
        agent=StorageAgent(hubroot,'msi',settings,dataset_root=self.base/'hub-dataset',log=quiet);agent.server=self.server
        self.server.archive.schedule_build(self.season)
        with patch('providers.httpClient.HttpClient._request',side_effect=AssertionError('Unexpected provider request')):agent.maintenance_once()
        manifest=self.reader.request('/v1/dataset')['manifest'];self.assertIsNotNone(manifest)
        output=self.base/'main-dataset';install_dataset(self.reader,manifest,output,self.base/'downloads')
        self.assertTrue((output/self.season/'player_match_stats.csv').is_file())
        for kind in ('player','team','match'):self.assertTrue((output/'concordance'/(kind+'_ids.csv')).is_file())
        self.assertFalse(list(output.rglob('raw')));self.assertEqual(self.server.archive.builds(),[])

    def test_transfer_copies_are_collected_only_after_verified_incorporation(self):
        mid=self.queue(True);self.outbox.send_one(self.peer);archive=self.server.archive
        archive.collected();self.assertTrue((archive.objects/digest(self.raw)).exists())
        manifest=archive.completed()[0][1];archive.incorporated(mid,manifest);archive.collected()
        self.assertFalse((archive.objects/digest(self.raw)).exists());self.assertTrue(self.raw.exists())
        self.assertEqual(archive.commit('headless',mid)['state'],'imported')

    def test_raw_capture_excludes_retired_generations_and_secrets(self):
        from tests.test_ensemble import populate
        populate(self.root,limit=1)
        atomic_json(self.root/'_system/config/secret.json',{'secret':'never transfer'})
        store=PandasStore(self.root/self.season/'match_checkpoints/1')
        dataset=store.from_csv();dataset.matches[1].metadata['raw_response_files']={'whoscored':'raw/whoscored/match.json'};store.to_csv(dataset)
        retired=store.directory/'snapshots'/('0'*32)/'matches.csv';retired.parent.mkdir();retired.write_text('retired')
        files=season_files(self.root,self.season,'1')
        self.assertTrue(any('/CURRENT.json' in p for p in files));self.assertFalse(any('_system' in p for p in files))
        self.assertTrue(any('/raw/' in p for p in files));self.assertFalse(any('0'*32 in p for p in files))

    def test_completed_remote_season_is_incorporated_and_worker_credit_retained(self):
        from tests.test_ensemble import populate
        from collection.workerIdentity import ensure_identity
        from collection.ensembleProgress import PortableProgress
        populate(self.root,limit=1);ensure_identity(self.root,'headless')
        self.outbox.enqueue(season_files(self.root,self.season),kind='raw',season=self.season,completed=True)
        self.outbox.send_one(self.peer)
        settings={'hub':'msi','consumers':['main'],'url':self.server.url}
        hubroot=self.base/'msi';atomic_json(hubroot/'.soccer-scraper.json',{'format':1});ensure_identity(hubroot,'msi')
        agent=StorageAgent(hubroot,'msi',settings,dataset_root=self.base/'hub-dataset',log=quiet);agent.server=self.server
        with patch('dataset_builder.build_dataset',return_value=2):
            with self.assertRaisesRegex(ValueError,'build failed'):agent.maintenance_once()
        self.assertTrue((hubroot/self.season/'match_checkpoints/1/CURRENT.json').exists())
        progress=PortableProgress(None,hubroot);progress.scan()
        self.assertTrue(any('headless' in r['origins'] for r in progress.local_records.values()))
        self.assertEqual(agent.server.archive.builds(),[self.season])
        self.assertTrue(agent.maintenance_changed.is_set())

    def test_a_new_build_request_cannot_be_lost_when_a_previous_build_finishes(self):
        archive=self.server.archive
        archive.schedule_build(self.season);_,revision=archive.build_jobs()[0]
        archive.schedule_build(self.season);archive.published(self.season,revision)
        self.assertEqual(archive.builds(),[self.season])
        _,revision=archive.build_jobs()[0];archive.published(self.season,revision)
        self.assertEqual(archive.builds(),[])

    def test_headless_uploads_during_a_blocked_background_dataset_copy(self):
        csv=self.base/'dataset.csv';csv.write_text('match_id,value\n1,new\n')
        manifest={'format':FORMAT,'kind':'dataset','worker':'msi','files':[
            {'path':self.season+'/player_stats.csv','size':csv.stat().st_size,'sha256':digest(csv)}]}
        copying=threading.Event();release=threading.Event();errors=[]
        import shutil
        original=shutil.copyfile
        def slow(source,target,*args,**kwargs):
            if Path(source)==csv:
                copying.set()
                if not release.wait(5):raise RuntimeError('Test did not release copy')
            return original(source,target,*args,**kwargs)
        def publish():
            try:self.server.archive.publish(manifest,{manifest['files'][0]['path']:csv})
            except BaseException as exc:errors.append(exc)
        with patch('collection.storageServer.shutil.copyfile',side_effect=slow):
            thread=threading.Thread(target=publish);thread.start()
            try:
                self.assertTrue(copying.wait(2))
                self.queue();self.assertTrue(self.outbox.send_one(self.peer))
                self.assertTrue(thread.is_alive())
            finally:release.set();thread.join(5)
        self.assertFalse(errors);self.assertFalse(thread.is_alive())


class ConfigurationTests(unittest.TestCase):
    def test_reporting_delivers_storage_config_and_still_installs_reep(self):
        from collection.reporting import ReportingServer,Reporter
        from collection.workerIdentity import ensure_identity
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);main=base/'main';worker=base/'worker';ensure_identity(worker,'msi')
            settings={'enabled':True,'hub':'msi','url':'http://100.116.229.51:8767','consumers':['main']}
            server=ReportingServer(main,base/'main-ensemble',{'bind':'127.0.0.1','port':0,'tokens':{'msi':'r'*40}},storage_plan=settings,workers=['msi','main','headless']).start()
            try:
                client=Reporter(worker,base/'ensemble','msi',{'worker_id':'msi','token':'r'*40,'url':'http://127.0.0.1:'+str(server.server.server_port)})
                real=client.request
                def with_reep(*args,**kwargs):
                    value=real(*args,**kwargs);value['reep_index']={'test':'payload'};return value
                with patch.object(client,'request',side_effect=with_reep),patch('providers.reep.install') as reep:
                    client.pull();reep.assert_called_once_with(worker,{'test':'payload'})
                config=json.loads(path_for(worker).read_text());self.assertEqual(config['role'],'hub');self.assertEqual(config['tokens']['main']['role'],'consumer')
            finally:server.close()

    def test_main_provisions_separate_roles_and_keeps_tokens_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);settings={'enabled':True,'hub':'msi','url':'http://100.116.229.51:8767','consumers':['main']}
            provision=Provisioner(root,settings,['main','msi','headless'])
            main=provision.for_worker('main');msi=provision.for_worker('msi');headless=provision.for_worker('headless')
            self.assertEqual((main['role'],msi['role'],headless['role']),('consumer','hub','producer'))
            self.assertNotEqual(main['token'],headless['token']);self.assertNotIn('tokens',main)
            install(root,main);self.assertEqual(json.loads(path_for(root).read_text()),main)
            self.assertEqual(Provisioner(root,settings,['main','msi','headless']).for_worker('main')['token'],main['token'])


if __name__=='__main__':unittest.main()

"""Old API collection remains complete when the next run uses a browser."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collection.seasonRunner import SeasonRunner
from collection.ensemble import ensemble_session
from collection.ensembleProgress import PortableProgress
from providers.transports.sofascore_extension import ExtensionSofaScoreClient
from storage import PandasStore
from storage.pandasStore import atomic_json
from tests.test_extension_payloads import extension_settings, quiet
from tests.test_extension_client import FakeBridge
from tests.test_providers import Sofa, event


def http_response(root, endpoint, payload, *, base='https://api.sofascore.com/api/v1', status=200):
    identity=json.dumps([base,endpoint,None,'json'],sort_keys=True)
    path=Path(root)/'http_cache'/'sofascore'/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
    atomic_json(path,{'identity':identity,'status':status,'payload':payload,'fetched_at':123})
    return path


class SofaScoreResumeTests(unittest.TestCase):
    def populate(self, root, *, spatial=()):
        directory=Path(root)/'premier-league'/'2024-2025'
        result=SeasonRunner({'sofascore':Sofa()},directory,providers=['sofascore'],spatial=spatial,report=quiet).run(
            'EPL','2024/25',limit=1,build_csvs=False)
        self.assertEqual(result.status.tolist(),['complete'])
        return directory

    def test_api_checkpoint_skips_extension_without_rewriting_or_connecting(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=self.populate(folder,spatial=('average_positions',))
            marker=directory/'match_checkpoints'/'1'/'CURRENT.json';before=marker.read_bytes()
            bridge=FakeBridge();client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            messages=[]
            result=SeasonRunner({'sofascore':client},directory,providers=['sofascore'],
                spatial=('average_positions',),report=messages.append).run('EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])
            self.assertEqual(marker.read_bytes(),before)
            self.assertEqual(bridge.calls,[])
            self.assertTrue(any('Already complete: 1' in text for text in messages))
            self.assertNotIn('browser_details',PandasStore(marker.parent).from_csv().matches[1].metadata)

    def test_api_checkpoint_skips_visible_browser_without_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=self.populate(folder)
            client=ExtensionSofaScoreClient(folder,extension_settings(),offline=True)
            client.uses_browser=True
            with patch.object(client.bridge,'request',side_effect=AssertionError('browser launched')):
                result=SeasonRunner({'sofascore':client},directory,providers=['sofascore'],spatial=(),report=quiet).run(
                    'EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])

    def test_new_spatial_requirement_only_fetches_the_missing_resource(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=self.populate(folder)
            bridge=FakeBridge();client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            with patch.object(client,'collect_details',side_effect=AssertionError('recollected player stats')):
                result=SeasonRunner({'sofascore':client},directory,providers=['sofascore'],
                    spatial=('average_positions',),report=quiet).run('EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])
            self.assertEqual(bridge.reads,['event/1/average-positions'])

    def test_explicitly_failed_browser_details_are_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=self.populate(folder)
            store=PandasStore(directory/'match_checkpoints'/'1');dataset=store.from_csv()
            dataset.matches[1].metadata['browser_details']={'complete':False,'errors':['denied']};store.to_csv(dataset)
            bridge=FakeBridge();client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=bridge,report=quiet)
            result=SeasonRunner({'sofascore':client},directory,providers=['sofascore'],spatial=(),report=quiet).run(
                'EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])
            self.assertIn('event/1/player/7/statistics',bridge.reads)
            self.assertTrue(store.from_csv().matches[1].metadata['browser_details']['complete'])

    def test_api_inventory_skips_extension_on_another_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/'first';self.populate(first)
            staging=root/'ensemble';progress=PortableProgress(staging/'provenance.json',first);progress.scan()
            second=root/'second';atomic_json(second/'.soccer-scraper.json',{'format':1});bridge=FakeBridge()
            client=ExtensionSofaScoreClient(second,extension_settings(),bridge=bridge,report=quiet)
            with ensemble_session(second,staging,quiet):
                result=SeasonRunner({'sofascore':client},second/'premier-league'/'2024-2025',
                    providers=['sofascore'],spatial=(),report=quiet).run('EPL','2024/25',limit=1,build_csvs=False)
            self.assertEqual(result.status.tolist(),['elsewhere'])
            self.assertFalse(any(key.startswith('event/') for key in bridge.reads))

    def test_http_cache_replays_in_both_browser_clients_without_network(self):
        for cls in (ExtensionSofaScoreClient,):
            for base in ('https://api.sofascore.com/api/v1','https://www.sofascore.com/api/v1'):
                with self.subTest(cls=cls,base=base),tempfile.TemporaryDirectory() as folder:
                    path=http_response(folder,'event/1',{'event':event(1)},base=base);before=path.read_bytes()
                    extra={'bridge':FakeBridge()} if cls is ExtensionSofaScoreClient else {}
                    client=cls(folder,extension_settings(),offline=True,report=quiet,**extra)
                    with patch.object(client.bridge,'request',side_effect=AssertionError('browser launched')):
                        self.assertEqual(client.event(1),{'event':event(1)})
                    self.assertEqual(path.read_bytes(),before)
                    self.assertFalse(client.cache.path('event/1',None,'json').exists())
                    if extra:
                        self.assertEqual(client.bridge.calls,[])
                        self.assertEqual(client.audit['event/1']['transport'],'http_cache')

    def test_browser_cache_can_be_reused_by_extension_and_detail_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            browser=ExtensionSofaScoreClient(folder,extension_settings())
            browser._save('event/1/statistics',{'statistics':[]},method='website_response')
            client=ExtensionSofaScoreClient(folder,extension_settings(),offline=True,bridge=FakeBridge(),report=quiet)
            self.assertEqual(client.statistics(1),{'statistics':[]})
            self.assertEqual(client.match_payloads(1),{'statistics':{'statistics':[]}})
            self.assertEqual(client.bridge.calls,[])

    def test_http_negative_cache_does_not_block_extension_and_refresh_bypasses_saved_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path=http_response(folder,'event/1',{},status=404)
            client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=FakeBridge(),report=quiet)
            self.assertEqual(client.event(1),{'event':event(1)})
            self.assertEqual(client.bridge.reads,['event/1'])
            http_response(folder,'event/1',{'event':event(1)})
            client.refresh_cache=True;client.event(1)
            self.assertEqual(client.bridge.reads,['event/1','event/1'])

    def test_invalid_shared_cache_identity_or_match_never_reaches_browser(self):
        for wrong_identity in (True,False):
            with self.subTest(wrong_identity=wrong_identity),tempfile.TemporaryDirectory() as folder:
                path=http_response(folder,'event/1',{'event':event(999)})
                if wrong_identity:
                    saved=json.loads(path.read_text());saved['identity']='wrong';atomic_json(path,saved)
                client=ExtensionSofaScoreClient(folder,extension_settings(),bridge=FakeBridge(),report=quiet)
                with self.assertRaises(ValueError):client.event(1)
                self.assertEqual(client.bridge.calls,[])


if __name__=='__main__':unittest.main()

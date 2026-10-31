from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from collection.ensembleProgress import PortableProgress
from collection.seasonRunner import SeasonRunner
from collection.whoScoredData import integrated_error
from providers.cache import ResponseCache
from providers.reep import install, FORMAT, _digest
from review_data import integrated_who
from storage.pandasStore import PandasStore, atomic_json
from tests.test_providers import Sofa


def quiet(*args,**kwargs):pass


class WhoScoredInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'data';self.source=self.root/'premier-league'/'2024-2025'
        SeasonRunner({'sofascore':Sofa()},self.source,providers=['sofascore'],spatial=(),report=quiet).run(
            'EPL','2024/25',limit=1,build_csvs=False)
        self.store=PandasStore(self.source/'match_checkpoints'/'1')
        dataset=self.store.from_csv();self.match=dataset.matches[1]
        self.raw=ResponseCache(self.source/'raw').path('whoscored','match/11')
        self.match.metadata['provider_match_ids']={'whoscored':11}
        self.match.metadata['raw_response_files']={'whoscored':self.raw.relative_to(self.source).as_posix()}
        self.store.to_csv(dataset)
        self.payload={'match_id':11,'matchCentreData':{
            'statusCode':6,'startDate':self.match.kickoff.date().isoformat(),
            'home':{'teamId':9001,'name':'Different home provider name'},
            'away':{'teamId':9002,'name':'Different away provider name'},
            'events':[{'eventId':1}]}}
        rows=[]
        for side,pid in [('home',9001),('away',9002)]:
            rows.extend([['team','sofascore',str(getattr(self.match,side+'_team').id),'rt'+side],
                         ['team','whoscored',str(pid),'rt'+side]])
        install(self.root,{'format':FORMAT,'version':_digest(rows),'release':'test','rows':rows})
        self.write(self.payload)

    def write(self,payload):
        atomic_json(self.raw,{'provider':'whoscored','key':'match/11','payload':payload})

    def test_startup_scan_accepts_reep_team_ids_despite_name_differences(self):
        self.assertIsNone(integrated_error(self.match,self.payload,root=self.root))
        progress=PortableProgress(None,self.root);progress.scan()
        self.assertTrue(any(r['namespace']=='whoscored_events' for r in progress.local_records.values()))
        self.assertTrue(integrated_who(self.source,1))

    def import_source(self,hub,*,batch='test'):
        from collection.backgroundImports import import_parts
        from collection.storageAgent import season_files
        from providers.reep import snapshot
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        install(hub,snapshot(self.root))
        return import_parts(hub,season_files(self.root,'premier-league/2024-2025'),owner='headless',batch=batch,report=quiet)

    def test_chunked_import_uses_receiver_reep_index_in_its_temporary_folder(self):
        hub=Path(self.temp.name)/'hub'
        original=self.raw.read_bytes()
        self.assertTrue(self.import_source(hub))
        imported=hub/self.raw.relative_to(self.root)
        self.assertEqual(imported.read_bytes(),original)
        progress=PortableProgress(None,hub);progress.scan()
        self.assertTrue(any(r['namespace']=='whoscored_events' for r in progress.local_records.values()))

    def test_chunked_import_still_rejects_wrong_team_with_same_display_name(self):
        hub=Path(self.temp.name)/'hub'
        payload=deepcopy(self.payload);payload['matchCentreData']['home']={'teamId':9002,'name':self.match.home_team.name}
        self.write(payload);original=self.raw.read_bytes()
        with self.assertRaisesRegex(ValueError,'home team differs'):self.import_source(hub)
        self.assertEqual(self.raw.read_bytes(),original)
        self.assertFalse((hub/'premier-league/2024-2025/match_checkpoints/1/CURRENT.json').exists())

    def test_sender_reep_index_cannot_authorize_unreviewed_ids_on_receiver(self):
        from collection.ensemble import import_folder
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        hub=Path(self.temp.name)/'hub';original=self.raw.read_bytes()
        with self.assertRaisesRegex(ValueError,'home team differs'):import_folder(self.root,hub,quiet)
        self.assertEqual(self.raw.read_bytes(),original)

    def test_wrong_team_with_same_name_still_fails_and_retains_raw_data(self):
        payload=deepcopy(self.payload)
        payload['matchCentreData']['home']={'teamId':9002,'name':self.match.home_team.name}
        self.write(payload);original=self.raw.read_bytes()
        with self.assertRaisesRegex(ValueError,'Invalid integrated WhoScored raw events: .*home team differs'):
            PortableProgress(None,self.root).scan()
        self.assertEqual(self.raw.read_bytes(),original)
        self.assertFalse(integrated_who(self.source,1))

    def test_cached_checkpoint_rechecks_changed_linked_raw_events(self):
        PortableProgress(None,self.root).scan()
        payload=deepcopy(self.payload);payload['matchCentreData']['startDate']='1999-01-01'
        self.write(payload)
        with self.assertRaisesRegex(ValueError,'date differs'):PortableProgress(None,self.root).scan()

    def test_cached_checkpoint_detects_previously_missing_raw_events(self):
        self.raw.unlink()
        first=PortableProgress(None,self.root);first.scan()
        self.assertFalse(any(r['namespace']=='whoscored_events' for r in first.local_records.values()))
        self.write(self.payload)
        second=PortableProgress(None,self.root);second.scan()
        self.assertEqual(second.scan_summary['validated'],1)
        self.assertTrue(any(r['namespace']=='whoscored_events' for r in second.local_records.values()))

    def test_wrong_match_date_unfinished_and_empty_events_still_fail(self):
        payloads=[]
        for key,value,reason in [('startDate','1999-01-01','date differs'),('statusCode',0,'not marked finished'),('events',[],'events are missing')]:
            payload=deepcopy(self.payload);payload['matchCentreData'][key]=value;payloads.append((payload,reason))
        payload=deepcopy(self.payload);payload['match_id']=999;payloads.append((payload,'match ID differs'))
        for payload,reason in payloads:
            with self.subTest(reason=reason):
                self.write(payload)
                with self.assertRaisesRegex(ValueError,reason):PortableProgress(None,self.root).scan()


if __name__=='__main__':unittest.main()

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

    def test_wrong_team_with_same_name_still_fails_and_retains_raw_data(self):
        payload=deepcopy(self.payload)
        payload['matchCentreData']['home']={'teamId':9002,'name':self.match.home_team.name}
        self.write(payload);original=self.raw.read_bytes()
        with self.assertRaisesRegex(ValueError,'Invalid integrated WhoScored raw events: .*home team differs'):
            PortableProgress(None,self.root).scan()
        self.assertEqual(self.raw.read_bytes(),original)
        self.assertFalse(integrated_who(self.source,1))

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

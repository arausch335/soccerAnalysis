from tests.paths import system_path
"""Transport compatibility, identity, fallback, rate limits and full-data coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import config
from collection.seasonRunner import SeasonRunner
from providers.transports.sofascore_payloads import validate_payload,event_url
from providers.transports.sofascore import SofaScoreTransport
from providers.httpClient import ProviderRequestError
from providers.safeClient import PersistentGate
from providers.networkSettings import make_session,save_cookies,redact
from tests.test_providers import Sofa,event,lineup
from storage.pandasStore import atomic_json


def quiet(*args,**kw):pass


def extension_settings():return {**config.TRANSPORT['extension'],'delay_min_seconds':0,'delay_max_seconds':0}


class ExtensionPayloadTests(unittest.TestCase):










    def test_invalid_spatial_response_and_duplicate_lineup_are_rejected(self):
        with self.assertRaises(ValueError):validate_payload('event/1/player/7/heatmap',{})
        payload=lineup();payload['away']['players'][0]['player']['id']=7
        with self.assertRaisesRegex(ValueError,'Duplicate'):validate_payload('event/1/lineups',payload)

    def test_http_profiles_persist_and_proxy_secret_is_never_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'TEST_SOCCER_PROXY':'http://alice:secret@localhost:8080'}):
            settings={**config.TRANSPORT['http'],'profiles':['chrome131'],'proxy_env':'TEST_SOCCER_PROXY'}
            one,first=make_session(tmp,'sofascore','https://www.sofascore.com/',settings)
            one.cookies.set('test','cookie',domain='www.sofascore.com');save_cookies(one);one.close()
            two,second=make_session(tmp,'sofascore','https://www.sofascore.com/',settings)
            self.assertEqual(first,second)
            self.assertEqual(two.cookies.get('test'),'cookie')
            text=(Path(tmp)/'_system/network/sessions/sofascore/identity.json').read_text()
            self.assertNotIn('secret',text);self.assertNotIn('alice',text)
            self.assertEqual(redact('http://alice:secret@localhost:8080'),'http://[redacted]@localhost:8080')
            two.close()




    def test_sofascore_team_statistics_are_exported_and_packaged_offline(self):
        from collection.sofascoreTeams import team_stat_rows
        from storage import PandasStore
        from providers.cache import ResponseCache
        from dataset_builder import package_collection
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'season'
            runner=SeasonRunner({'sofascore':Sofa()},directory,providers=['sofascore'],spatial=(),report=quiet)
            runner.run('EPL','2024/25',limit=1,build_csvs=False)
            store=PandasStore(directory/'match_checkpoints'/'1');dataset=store.from_csv();match=dataset.matches[1]
            cache=ResponseCache(directory/'raw')
            payload={'statistics':[{'period':'ALL','groups':[{'groupName':'Overview','statisticsItems':[
                {'name':'Ball possession','key':'ballPossession','homeValue':60,'awayValue':40}]}]}]}
            cache.fetch('sofascore','statistics/1',lambda:payload)
            match.metadata['raw_response_files']={'sofascore_statistics':str(cache.path('sofascore','statistics/1').relative_to(directory))}
            store.to_csv(dataset)
            rows=team_stat_rows(match,directory)
            self.assertEqual([row['value'] for row in rows],[60,40])
            self.assertEqual({row['provider'] for row in rows},{'sofascore'})
            runner._export([event(1)],compile_events=False)
            combined=pd.read_csv(directory/'team_match_statistics.csv')
            self.assertEqual(combined.value.tolist(),[60,40])
            target=package_collection(directory,Path(tmp)/'packaged',tables=('team_match_stats',))
            packaged=pd.read_csv(target/'team_match_stats.csv')
            self.assertEqual(packaged.provider.tolist(),['sofascore','sofascore'])

    def test_native_match_url_preserves_custom_and_event_ids(self):
        value=event(1);value['customId']='NsU'
        self.assertTrue(event_url(value).endswith('/NsU#id:1'))


if __name__=='__main__':unittest.main()

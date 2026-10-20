from tests.paths import system_path
"""WhoScored native exports, offline integration and transport identity contracts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import config
from collection.ensemble import ensemble_session, import_pending
from collection.ensembleProgress import job_key
from collection.seasonRunner import SeasonRunner
from tests.legacy_collectors import WhoScoredEventRunner
from collection.whoScoredExports import export_standalone, package_standalone
from collection.whoScoredData import standalone_directory
from providers.cache import ResponseCache
from providers.httpClient import ProviderRequestError, ProviderError
from providers.whoScoredClient import WhoScoredClient, embedded_json
from providers.transports.whoscored import WhoScoredTransport, ExtensionWhoScoredClient, WhoScoredExtensionGate
from storage.pandasStore import atomic_json, PandasStore
from tests.test_providers import Sofa, event


def quiet(*a,**kw):pass


def fixture():
    result=deepcopy(event());result['id']=11
    result['homeTeam']={'id':501,'name':'Liverpool'};result['awayTeam']={'id':502,'name':'Other'}
    return result


def payload():
    return {'match_id':11,'matchCentreEventTypeJson':{'passAccurate':1,'passInaccurate':2},
        'matchCentreData':{'statusCode':6,'startDate':'2024-08-17','events':[
            {'id':99,'eventId':7,'teamId':501,'playerId':77,'satisfiedEventsTypes':[1],
             'type':{'displayName':'Pass','value':1},'future':'preserved'}],
            **{side:{'teamId':tid,'name':team,'scores':{'fulltime':0},'stats':{'rating':{'90':7}},'players':[
                {'playerId':pid,'name':name,'shirtNo':7,'isFirstEleven':True}], 'formations':[]}
               for side,tid,pid,name,team in [('home',501,77,'Player One','Liverpool'),('away',502,88,'Player Two','Other')]}}}


def populate(root):
    atomic_json(Path(root)/'.soccer-scraper.json',{'format':1})
    directory=standalone_directory(root,'EPL','2024/25')
    atomic_json(directory/'whoscored_job.json',{'provider':'whoscored','competition':'Premier League','season':'2024/2025'})
    atomic_json(directory/'whoscored_schedule.json',{'provider':'whoscored','events':[fixture()]})
    ResponseCache(directory/'raw').fetch('whoscored','match/11',payload)
    return directory


class WhoScoredIntegrationTests(unittest.TestCase):
    def test_standalone_export_preserves_namespaced_ids_unknown_events_and_native_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=populate(Path(tmp)/'data')
            self.assertEqual(export_standalone(directory),1)
            frame=pd.read_csv(directory/'whoscored_events.csv');self.assertEqual(frame.wsc_match_id.tolist(),[11])
            self.assertEqual(frame.wsc_player_id.tolist(),[77]);self.assertNotIn('match_id',frame)
            self.assertEqual(json.loads(frame.extra_event_json[0]),{'future':'preserved'})
            stats=pd.read_csv(directory/'whoscored_player_match_stats.csv');self.assertEqual(stats.wsc_passes_completed.tolist(),[1,0])
            self.assertTrue(stats.wsc_shots.isna().all())
            target=package_standalone(directory,Path(tmp)/'dataset')
            self.assertEqual(target,Path(tmp)/'dataset'/'whoscored'/'premier-league'/'2024-2025')
            self.assertEqual((target/'events.csv').read_bytes(),(directory/'whoscored_events.csv').read_bytes())

    def test_standalone_data_enriches_combined_checkpoints_and_offline_exports_without_whoscored_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';populate(root);directory=root/'premier-league'/'2024-2025'
            who=Mock();who.seasons.side_effect=AssertionError('No WhoScored network discovery')
            who.match_details.side_effect=AssertionError('No WhoScored download')
            sofa=Sofa();sofa.count=1
            runner=SeasonRunner({'sofascore':sofa,'whoscored':who},directory,
                providers=['sofascore','whoscored'],spatial=(),report=quiet)
            result=runner.run('EPL','2024/25',build_csvs=False)
            self.assertEqual(result.status.tolist(),['complete'])
            match=PandasStore(directory/'match_checkpoints'/'1').from_csv().matches[1]
            self.assertEqual(match.metadata['provider_match_ids']['whoscored'],11)
            self.assertEqual(match.home_lineup.players[0].stats.raw['_providers']['whoscored']['metrics']['passes_completed'],1)
            who.seasons.assert_not_called();who.match_details.assert_not_called()
            runner._export([event()],compile_events=True)
            self.assertEqual(pd.read_csv(directory/'events.csv').provider_match_id.tolist(),[11])
            with ensemble_session(root,Path(tmp)/'ensemble',quiet,worker_id='mac') as progress:
                self.assertIn(job_key('whoscored_events','Premier League','2024/25',11),progress.local_records)


    def test_existing_sofascore_only_checkpoints_gain_whoscored_during_offline_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';directory=root/'premier-league'/'2024-2025';sofa=Sofa();sofa.count=1
            runner=SeasonRunner({'sofascore':sofa},directory,providers=['sofascore'],spatial=(),report=quiet)
            runner.run('EPL','2024/25',build_csvs=False);populate(root)
            runner._export([event()],compile_events=True)
            match=runner.dataset.matches[1]
            self.assertEqual(match.metadata['provider_status']['whoscored'],'complete')
            self.assertEqual(match.metadata['provider_match_ids']['whoscored'],11)
            self.assertEqual(len(pd.read_csv(directory/'events.csv')),1)


    def test_import_keeps_one_exact_event_response_instead_of_synthesizing_a_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);live=base/'data';source=base/'ensemble'/'msi'/'data'
            first=populate(live);second=populate(source)
            path=next((second/'raw'/'whoscored').glob('*.json'));saved=json.loads(path.read_text())
            saved['payload']['matchCentreData']['events'][0]['future']='other computer'
            atomic_json(path,saved)
            original=next((first/'raw'/'whoscored').glob('*.json')).read_bytes()
            import_pending(live,base/'ensemble',quiet)
            self.assertEqual(next((first/'raw'/'whoscored').glob('*.json')).read_bytes(),original)
            self.assertFalse(source.exists())


class WhoScoredTransportTests(unittest.TestCase):


    def test_extension_payload_is_parsed_and_wrong_match_or_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge=Mock();p=payload();text='matchId: 11, matchCentreData: '+json.dumps(p['matchCentreData'])+', matchCentreEventTypeJson: '+json.dumps(p['matchCentreEventTypeJson'])
            bridge.request.return_value={'ok':True,'status':200,'source_url':'https://www.whoscored.com/matches/11/live','text':text}
            client=ExtensionWhoScoredClient(tmp,bridge=bridge)
            with patch.object(client.request_gate,'wait'):
                self.assertEqual(client.match_details(11)['match_id'],11)
                with self.assertRaisesRegex(ProviderError,'different or incomplete'):client.match_details(12)
                bridge.request.return_value['source_url']='https://evil.test/'
                with self.assertRaisesRegex(ProviderError,'left'):client.match_details(13)
            bridge.request.assert_called_with('whoscored',unittest.mock.ANY,timeout=unittest.mock.ANY)

    def test_parser_accepts_assignments_and_quoted_keys_without_executing_javascript(self):
        self.assertEqual(embedded_json('var matchCentreData = {"unicode":"Łódź"};','matchCentreData'),{'unicode':'Łódź'})
        self.assertEqual(embedded_json('"matchCentreData": {"x":1}','matchCentreData'),{'x':1})
        with self.assertRaises(ProviderError):embedded_json('matchCentreData: runCode()','matchCentreData')

    def test_browser_ignores_http_verification_but_shares_rate_limit(self):
        from providers.safeClient import PersistentGate
        with tempfile.TemporaryDirectory() as tmp:
            http=PersistentGate(system_path(Path(tmp),"safety"),'whoscored');http.block(ProviderRequestError('HTTP 403',status_code=403))
            browser=WhoScoredExtensionGate(tmp);self.assertFalse(browser.is_blocked)
            http.block(ProviderRequestError('HTTP 429',status_code=429));self.assertTrue(browser.is_blocked)
            with self.assertRaises(ProviderRequestError):browser.wait('matches/11/live')

    def test_http_pages_decode_utf8_even_when_platform_text_decoding_would_fail(self):
        response=Mock();response.content='matchCentreData: {"name":"Łódź"}'.encode('utf-8')
        client=WhoScoredClient(session=Mock())
        with patch.object(client,'_request',return_value=response):
            self.assertEqual(client.get_text('matches/11/live'),'matchCentreData: {"name":"Łódź"}')

class WhoScoredLayoutMigrationTests(unittest.TestCase):
    def legacy(self,root):
        canonical=populate(root)
        old=Path(root)/'whoscored_events'/canonical.parent.name/canonical.name
        old.parent.mkdir(parents=True);canonical.rename(old)
        (old/'whoscored_job.json').rename(old/'job.json')
        (old/'whoscored_schedule.json').rename(old/'schedule.json')
        return old,canonical

    def test_legacy_layout_migrates_without_changing_baseline_schedule_or_raw_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';old,canonical=self.legacy(root)
            original=next((old/'raw/whoscored').glob('*.json')).read_bytes()
            atomic_json(canonical/'schedule.json',{'events':[event()]})
            atomic_json(canonical/'job.json',{'baseline_provider':'sofascore'})
            baseline=(canonical/'schedule.json').read_bytes()
            with ensemble_session(root,Path(tmp)/'ensemble',quiet,worker_id='mac') as progress:
                self.assertEqual(len(progress.local_records),1)
            self.assertFalse((root/'whoscored_events').exists())
            self.assertEqual((canonical/'schedule.json').read_bytes(),baseline)
            self.assertTrue((canonical/'whoscored_schedule.json').exists())
            self.assertEqual(next((canonical/'raw/whoscored').glob('*.json')).read_bytes(),original)
            with ensemble_session(root,Path(tmp)/'ensemble',quiet):pass

    def test_transferred_legacy_folder_is_imported_into_canonical_provider_raw_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=base/'ensemble/msi/data';self.legacy(source)
            import_pending(base/'data',base/'ensemble',quiet)
            self.assertFalse(source.exists())
            self.assertFalse((base/'data/whoscored_events').exists())
            directory=base/'data/premier-league/2024-2025'
            self.assertEqual(len(list((directory/'raw/whoscored').glob('*.json'))),1)
            self.assertEqual(export_standalone(directory),1)

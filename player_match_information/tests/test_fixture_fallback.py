"""Fixture fallback switches a whole output without mixing provider IDs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from api.httpClient import ProviderRequestError
from api.safeClient import PersistentGate, SafeSofaScoreClient, SafeFotMobClient
from build_fixtures import FixtureCompetition, build
from dataset_manager import _fixture_provider, build_id_tables
from test_build_fixtures import FakeScheduleClient, FakeFotMobClient


class BlockedSofa(FakeScheduleClient):
    def __init__(self,status=403):
        super().__init__();self.error=ProviderRequestError(f'HTTP {status}',status_code=status)
    def seasons(self,cid):
        self.calls.append(('blocked',cid))
        raise self.error


class YearsFotMob(FakeFotMobClient):
    def seasons(self,cid):
        self.calls.append(('seasons',cid));return ['2010/2011','2011/2012']
    def season_events(self,cid,season):
        rows=super().season_events(cid,season)
        for row in rows:row['id']=str(100+int(season[:4])-2010)
        return rows


class FixtureFallbackTests(unittest.TestCase):
    def options(self,root,sofa,fot,end=2010):
        return dict(client=sofa,fallback_client=fot,data_root=root/'data',
            output_csv=root/'dataset/fixtures.csv',
            competitions=(FixtureCompetition('Premier League',17,fotmob_id=47),),
            start_season=2010,end_season=end,refresh_current=False,
            now=datetime(2026,10,9,tzinfo=timezone.utc))

    def test_default_starts_with_sofascore_then_switches_after_403(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sofa=BlockedSofa();fot=FakeFotMobClient()
            output=build(**self.options(root,sofa,fot))
            self.assertEqual(sofa.calls,[('blocked',17)])
            self.assertIn(('events',47,'2010/2011',False),fot.calls)
            self.assertEqual(_fixture_provider(output),'fotmob')
            self.assertEqual(pd.read_csv(output).match_id.tolist(),[47])
            metadata=json.loads(output.with_suffix('.metadata.json').read_text())
            self.assertEqual(metadata['requested_provider'],'sofascore')
            self.assertIn('403',metadata['fallback_reason'])
            self.assertTrue((root/'data/.soccer-scraper.json').exists())
            # Concordance IDs must follow the actual provider, not the requested one.
            paths=build_id_tables(root/'data',root/'dataset')
            matches=pd.read_csv(paths['match IDs'],dtype=str,keep_default_na=False)
            self.assertEqual(matches.fotmob_id.tolist(),['47'])
            self.assertEqual(matches.sofascore_id.tolist(),[''])

    def test_mid_build_403_restarts_on_fotmob_and_retains_sofa_checkpoints(self):
        class LaterBlockedSofa(FakeScheduleClient):
            def iter_season_events(self,cid,sid,**kwargs):
                if sid==11:
                    self.calls.append(('blocked-events',sid))
                    raise ProviderRequestError('HTTP 403',status_code=403)
                return super().iter_season_events(cid,sid,**kwargs)

        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sofa=LaterBlockedSofa();fot=YearsFotMob()
            options=self.options(root,sofa,fot,end=2011)
            output=build(**options)
            frame=pd.read_csv(output)
            self.assertEqual(frame.match_id.tolist(),[100,101])
            self.assertEqual(set(frame.home_team_id),{4})
            self.assertEqual(_fixture_provider(output),'fotmob')
            self.assertTrue((root/'data/fixture_schedules/sofascore/premier-league/2010-2011.json').exists())
            sofa_calls=list(sofa.calls);fot_calls=list(fot.calls)
            build(**options)
            self.assertEqual(sofa.calls,sofa_calls)
            self.assertEqual(fot.calls,fot_calls)

    def test_other_failures_do_not_trigger_fallback(self):
        for status in (401,404,429,500,None):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fot=FakeFotMobClient()
                with self.assertRaises(ProviderRequestError):
                    build(**self.options(root,BlockedSofa(status),fot))
                self.assertEqual(fot.calls,[])
                self.assertFalse((root/'dataset/fixtures.csv').exists())

    def test_disabled_fallback_propagates_403(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fot=FakeFotMobClient()
            with self.assertRaises(ProviderRequestError):
                build(**self.options(root,BlockedSofa(),fot),fallback_on_403=False)
            self.assertEqual(fot.calls,[])

    def test_fotmob_failure_keeps_previous_output_and_metadata(self):
        class BlockedFot(FakeFotMobClient):
            def seasons(self,cid):raise ProviderRequestError('HTTP 403',status_code=403)

        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'dataset/fixtures.csv';output.parent.mkdir()
            output.write_text('previous output\n');metadata=output.with_suffix('.metadata.json')
            metadata.write_text('previous metadata\n')
            with self.assertRaises(ProviderRequestError):
                build(**self.options(root,BlockedSofa(),BlockedFot()))
            self.assertEqual(output.read_text(),'previous output\n')
            self.assertEqual(metadata.read_text(),'previous metadata\n')

    def test_existing_sofascore_403_cooldown_selects_fotmob_without_retrying(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sofa=FakeScheduleClient();fot=FakeFotMobClient()
            PersistentGate(root/'data/safety','sofascore').block(
                ProviderRequestError('HTTP 403',status_code=403))
            build(**self.options(root,sofa,fot))
            self.assertEqual(sofa.calls,[])
            self.assertNotEqual(fot.calls,[])

    def test_retry_after_survives_restart_and_expiry_retries_sofascore(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sofa=BlockedSofa();sofa.error.retry_after=172800
            fot=FakeFotMobClient()
            with patch('build_fixtures.time.time',return_value=1000):
                build(**self.options(root,sofa,fot))
            state=json.loads((root/'data/fixture_schedules/fallback.json').read_text())
            self.assertEqual(state['blocked_until'],173800)
            working=FakeScheduleClient()
            with patch('build_fixtures.time.time',return_value=1000+86401):
                build(**self.options(root,working,fot))
            self.assertEqual(working.calls,[])
            with patch('build_fixtures.time.time',return_value=173801):
                output=build(**self.options(root,working,fot))
            self.assertIn(('seasons',17),working.calls)
            self.assertEqual(_fixture_provider(output),'sofascore')

    def test_real_safe_clients_keep_global_pacing_when_switching_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);clock=[1000.0];starts=[];sleeps=[]
            sofa_session=Mock();fot_session=Mock()

            def response(status,payload):
                result=Mock(status_code=status,headers={})
                result.json.return_value=payload
                return result

            def sofa_request(*args,**kwargs):
                starts.append(('sofascore',clock[0]))
                return response(403,{'error':{'reason':'challenge'}})

            payloads=iter([
                {'allAvailableSeasons':['2010/2011']},
                {'allAvailableSeasons':['2010/2011'],
                 'overview':{'selectedSeason':'2010/2011'},
                 'fixtures':{'allMatches':FakeFotMobClient().season_events(47,'2010/2011')}}])

            def fot_request(*args,**kwargs):
                starts.append(('fotmob',clock[0]))
                return response(200,next(payloads))

            def sleep(seconds):
                sleeps.append(seconds);clock[0]+=seconds

            sofa_session.get.side_effect=sofa_request;fot_session.get.side_effect=fot_request
            with patch('api.safeClient.time.time',side_effect=lambda:clock[0]), \
                    patch('api.safeClient.time.sleep',side_effect=sleep), \
                    patch('api.safeClient.random.uniform',return_value=10):
                with SafeSofaScoreClient(root/'data','sofascore',session=sofa_session) as sofa, \
                        SafeFotMobClient(root/'data','fotmob',session=fot_session) as fot:
                    output=build(**self.options(root,sofa,fot))
                    self.assertTrue(sofa.request_gate.is_blocked)
                    self.assertEqual(sofa.request_gate.read()['status_code'],403)
            self.assertEqual(starts,[('sofascore',1000),('fotmob',1010),('fotmob',1020)])
            self.assertEqual(sleeps,[10,10])
            self.assertEqual(_fixture_provider(output),'fotmob')

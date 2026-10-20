from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from providers.identityReview import ReviewStore,identity_scope,suggest_players,revision
from providers.identity import IdentityResolver,IdentityError,match_fixture,team_key
from providers.whoScoredClient import embedded_json
from providers.httpClient import ProviderError
from tests import test_providers as provider_tests
from tests.test_providers import fot_payload,event,lineup,Sofa,Fot
from providers import FotMobLoader
from collection.seasonRunner import SeasonRunner
from tests import test_reporting
from tests import test_dashboard
from tests import test_worker
from tests.test_worker import NoSync


def proposal(**values):
    return dict(kind='player',provider='fotmob',source_id='77',target_id='7',
                source_name='N. Uno',target_name='Player One',score=.8,
                contexts=[{'match_id':'1','match':'Liverpool vs Other','side':'home'}],**values)


class IdentityReviewTests(unittest.TestCase):
    def test_confirmed_id_is_global_but_resolver_still_requires_same_lineup(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            store=ReviewStore(tmp);store.ingest([proposal()],'msi')
            p=store.snapshot()['pending'][0];self.assertEqual(p['contexts'][0]['worker'],'msi')
            appearance=SimpleNamespace(player=SimpleNamespace(id=7,name='Player One'),shirt_number=7)
            entry={'id':77,'name':'N. Uno','shirt_number':7}
            resolver=IdentityResolver()
            with self.assertRaises(IdentityError):resolver.player('fotmob',entry,[appearance])
            store.decide(p['id'],'confirm')
            self.assertEqual(resolver.player('fotmob',entry,[appearance])[0],appearance)
            with self.assertRaises(IdentityError):resolver.player('fotmob',entry,[])
            self.assertEqual(store.snapshot()['pending'],[]);self.assertTrue(revision(tmp))
            with self.assertRaises(IdentityError):resolver.player('whoscored',entry,[appearance])

    def test_rejection_persists_without_becoming_a_mapping_and_conflicts_are_rejected(self):
        with TemporaryDirectory() as tmp:
            store=ReviewStore(tmp);store.ingest([proposal()])
            p=store.snapshot()['pending'][0];store.decide(p['id'],'reject')
            self.assertFalse(revision(tmp));self.assertEqual(store.snapshot()['pending'],[])
            with identity_scope(tmp),self.assertRaises(IdentityError):
                IdentityResolver().player('fotmob',{'id':77,'name':'Player One'},[SimpleNamespace(player=SimpleNamespace(id=7,name='Player One'))])
            store.decide(p['id'],'confirm')
            other={**proposal(),'target_id':'8','target_name':'Another Player'}
            store.ingest([other]);other=next(p for p in store.proposals() if p['target_id']=='8')
            with self.assertRaises(ValueError):store.decide(other['id'],'confirm')

    def test_team_suggestion_has_same_date_side_and_known_opponent_anchor(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            event={'id':1,'startTimestamp':1720000000,'homeTeam':{'name':'SpVgg Greuther Fürth'},'awayTeam':{'name':'Bayern'}}
            fixture={**event,'id':101,'homeTeam':{'name':'Greuther Fürth'}}
            with self.assertRaises(IdentityError):match_fixture(event,[fixture],provider='fotmob')
            store=ReviewStore(tmp);p=store.snapshot()['pending'][0]
            self.assertEqual(p['kind'],'team');self.assertEqual(p['contexts'][0]['side'],'home')
            store.decide(p['id'],'confirm')
            self.assertEqual(match_fixture(event,[fixture])['id'],101)
            self.assertEqual(team_key('SpVgg Greuther Fürth'),team_key('Greuther Fürth'))
            with self.assertRaises(IdentityError):match_fixture(event,[{**fixture,'startTimestamp':1720000000+86400}])

    def test_enrichment_queues_suggestions_and_revalidation_repairs_cached_player_rows(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            match=provider_tests.ProviderTests().baseline();payload=fot_payload()
            payload['content']['lineup']['homeTeam']['starters'][0]['name']='N. Uno'
            loader=FotMobLoader();loader.enrich(match,payload,allow_partial=True)
            self.assertTrue(match.metadata['identity_mismatches']['fotmob'])
            store=ReviewStore(tmp);p=store.snapshot()['pending'][0];self.assertEqual(p['target_id'],'7')
            store.decide(p['id'],'confirm');loader.enrich(match,payload,allow_partial=True)
            self.assertEqual(match.metadata['identity_mismatches']['fotmob'],[])
            self.assertEqual(match.home_lineup.players[0].stats.raw['_providers']['fotmob']['identity_method'],'dashboard_reviewed_provider_id')

    def test_incomplete_null_assignment_is_skipped_without_executing_javascript(self):
        self.assertEqual(embedded_json('matchCentreData = null; matchCentreData = {"events": []}','matchCentreData'),{'events':[]})
        with self.assertRaises(ProviderError):embedded_json('matchCentreData=null','matchCentreData')
        with self.assertRaises(ProviderError):embedded_json('matchCentreData={"a":1}; matchCentreData={"a":2}','matchCentreData')

    def test_fixture_discovery_keeps_successful_months_when_later_month_fails(self):
        from tests.test_whoscored_events import WhoScoredOnly
        from providers.httpClient import ProviderRequestError
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            who=WhoScoredOnly()
            # A configured stage avoids unrelated season lookup in this test.
            who.season_events=lambda stage,month: (_ for _ in ()).throw(ProviderRequestError('HTTP 502',status_code=502)) if month=='202409' else [{'id':11,'status':6,'startTime':'2024-08-17T12:00:00','home':{'id':1,'name':'Liverpool'},'away':{'id':2,'name':'Other'},'homeScore':0,'awayScore':0}]
            # Fixture schema is covered by existing provider tests; inject conversion here.
            with patch('collection.seasonRunner.configured_whoscored_stages',return_value=[1]),patch('collection.seasonRunner.months_between',return_value=['202408','202409']),patch('collection.seasonRunner.whoscored_fixture',return_value=event(11)):
                runner=SeasonRunner({'sofascore':Sofa(),'whoscored':who},tmp,providers=['whoscored'],spatial=(),report=lambda *a,**k:None)
                from collection.selection import competition_by_name
                fixtures,errors=runner._fixtures(competition_by_name('EPL'),'2024/2025',[event()])
            self.assertEqual([f['id'] for f in fixtures['whoscored']],[11]);self.assertNotIn('whoscored',errors)

    def test_historical_csv_decimal_ids_seed_real_candidates(self):
        import csv
        from storage import PandasStore
        from providers.identityReview import seed_existing
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            directory=Path(tmp)/'premier-league'/'2024-2025';directory.mkdir(parents=True)
            match=provider_tests.ProviderTests().baseline()
            from providers import SofaScoreLoader
            loader=SofaScoreLoader(None);season=loader.select_season(17,'Premier League',61627,'24/25')
            loader._build_match(season,event(),lineup())
            PandasStore(directory/'match_checkpoints'/'1').to_csv(loader.dataset)
            row={'match_id':'1','provider':'fotmob','provider_player_id':'77.0','provider_player_name':'Plyr One',
                 'issue':'fotmob: cannot uniquely match Plyr One','side':'home'}
            with (directory/'identity_mismatches.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            store=seed_existing(tmp);p=store.snapshot()['pending'][0]
            self.assertEqual(p['source_id'],'77');self.assertEqual(p['target_id'],'7')

    def test_transient_discovery_retries_between_matches_and_keeps_prior_partial(self):
        with TemporaryDirectory() as tmp,identity_scope(tmp):
            runner=SeasonRunner({'sofascore':Sofa(),'fotmob':Fot()},tmp,providers=['fotmob'],spatial=(),report=lambda *a,**k:None)
            from collection.seasonRunner import fotmob_fixture
            fixtures=[fotmob_fixture(f) for f in Fot().season_events()]
            times=iter((0,0,400,400))
            with patch.object(runner,'_fixtures',side_effect=[({}, {'fotmob':'FotMob HTTP 502'}),({'fotmob':fixtures},{})]) as discovery,patch('time.monotonic',side_effect=lambda:next(times)):
                result=runner.run('EPL','2024/25',limit=2,build_csvs=False)
            self.assertEqual(list(result.status),['partial','complete'])
            self.assertEqual(discovery.call_count,2)

    def test_access_errors_do_not_retry_fixture_discovery(self):
        for code in ('401','403','429'):
            with TemporaryDirectory() as tmp,identity_scope(tmp):
                runner=SeasonRunner({'sofascore':Sofa(),'fotmob':Fot()},tmp,providers=['fotmob'],spatial=(),report=lambda *a,**k:None)
                with patch.object(runner,'_fixtures',return_value=({}, {'fotmob':'HTTP '+code})) as discovery,patch('time.monotonic',side_effect=[0,400,800]):
                    result=runner.run('EPL','2024/25',limit=2,build_csvs=False)
                self.assertEqual(list(result.status),['partial','partial']);self.assertEqual(discovery.call_count,1)


class IdentityReportingTests(unittest.TestCase):
    setUp=test_reporting.ReportingTests.setUp
    reporter=test_reporting.ReportingTests.reporter
    receiver=test_reporting.ReportingTests.receiver

    def test_suggestions_send_durably_and_decisions_return_to_worker(self):
        server=self.receiver();client=self.reporter();local=ReviewStore(self.root);local.ingest([proposal()])
        client.enqueue(reset=True);client.flush()
        p=server.store.identities.snapshot()['pending'][0]
        self.assertEqual(p['contexts'][0]['worker'],'msi')
        server.store.identities.decide(p['id'],'confirm');client.pull()
        self.assertEqual(ReviewStore(self.root).snapshot()['decisions'][0]['action'],'confirm')
        with identity_scope(self.root):self.assertTrue(revision())
        restarted=self.reporter();restarted.enqueue();restarted.flush()
        self.assertEqual(len(server.store.identities.proposals()),1)


class IdentityDashboardTests(unittest.TestCase):
    setUp=test_dashboard.DashboardTests.setUp

    def test_review_requires_same_origin_token_and_main_and_survives_restart(self):
        server=__import__('dashboard.server',fromlist=['start_server']).start_server(self.root,self.ensemble,self.plan,'main',0)
        self.addCleanup(server.close)
        store=ReviewStore(self.root);store.ingest([proposal()]);pid=store.snapshot()['pending'][0]['id']
        with urlopen(server.url+'/api/identity-review') as response:value=json.load(response)
        self.assertTrue(value['can_review'])
        body=json.dumps({'id':pid,'action':'confirm'}).encode()
        for origin,token in [('https://evil.test',value['review_token']),(server.url,'wrong')]:
            req=Request(server.url+'/api/identity-review',data=body,headers={'Content-Type':'application/json','Origin':origin,'X-Review-Token':token})
            with self.assertRaises(HTTPError) as error:urlopen(req)
            self.assertEqual(error.exception.code,403)
        req=Request(server.url+'/api/identity-review',data=body,headers={'Content-Type':'application/json','Origin':server.url,'X-Review-Token':value['review_token']})
        with urlopen(req) as response:self.assertTrue(json.load(response)['saved'])
        self.assertEqual(len(ReviewStore(self.root).snapshot()['decisions']),1)
        from dashboard.model import DashboardModel
        with self.assertRaises(ValueError):DashboardModel(self.root,self.ensemble,self.plan,'msi').decide_identity({'id':pid,'action':'confirm'})


class IdentityWorkerTests(unittest.TestCase):
    setUp=test_worker.WorkerTests.setUp
    save=test_worker.WorkerTests.save
    session=test_worker.WorkerTests.session

    def test_new_confirmation_retries_only_incomplete_jobs_once_per_revision(self):
        calls=[]
        def runner(args):calls.append(args);return 2 if 'La Liga' in args else 0
        from collection.worker import run
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),2)
        before=len(calls);store=ReviewStore(self.root);store.ingest([proposal()]);p=store.snapshot()['pending'][0]
        store.decide(p['id'],'confirm')
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),2)
        self.assertEqual(len(calls),before+1)
        run(self.args,runner=runner,session=self.session,sync_factory=NoSync)
        self.assertEqual(len(calls),before+1)

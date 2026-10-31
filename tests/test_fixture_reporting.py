"""Fixture progress reaches Main without replacing stats or inventing coverage."""
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collection.reporting import Reporter,ReportingStore
from collection.workerIdentity import ensure_identity
from fixtures.progress import Progress,path_for
from dashboard.fixtureProgress import view
from storage.pandasStore import atomic_json


class FixtureReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.ensemble=self.base/'ensemble'
        ensure_identity(self.root,'headless')
        self.options={'worker_id':'headless','url':None,'token':''}
        self.reporter=Reporter(self.root,self.ensemble,'headless',self.options)

    def fixture(self):
        with Progress(self.root,self.ensemble,2,network=False) as progress:
            progress('season_started',competition='Europa League',season='2017-2018')
            progress('provider_started',provider='whoscored')
            progress('provider_progress',stage='Group L',month='201712',current_fixtures=24)
            return dict(progress.value)

    def events(self):
        with self.reporter.connect() as db:rows=db.execute('SELECT id,payload FROM queue ORDER BY id').fetchall()
        return [{**json.loads(payload),'instance_id':self.reporter.identity['instance_id'],
                 'stream':self.reporter.stream,'sequence':seq} for seq,payload in rows]

    def test_fixture_only_report_survives_a_later_stats_heartbeat(self):
        fixture=self.fixture();store=ReportingStore(self.base/'main',self.base/'main-ensemble')
        self.reporter.enqueue_fixture(fixture);store.accept('headless',self.events())
        stats={'format':'soccer-worker-runtime-v1','worker_id':'headless','state':'waiting_for_extension',
               'updated_at':'2026-10-23T18:29:01+00:00','job':{'competition':'Serie A','season':'2022-2023'}}
        atomic_json(self.root/'_system/workers/headless.json',stats)
        self.reporter.enqueue(inventory=False);store.accept('headless',self.events())
        state=json.loads((self.base/'main-ensemble/reporting/runtime/headless.json').read_text())
        self.assertEqual(state['job'],stats['job']);self.assertEqual(state['fixture_run']['stage'],'Group L')
        self.assertEqual(state['state'],'waiting_for_extension')
        received=state['fixture_run']['received_at']
        # Stats reports must not refresh a fixture heartbeat from a stopped CLI.
        self.reporter.enqueue(inventory=False);store.accept('headless',self.events())
        state=json.loads((self.base/'main-ensemble/reporting/runtime/headless.json').read_text())
        self.assertEqual(state['fixture_run']['received_at'],received)

    def test_coalescing_keeps_logs_and_final_fixture_state_durable(self):
        self.fixture()
        with Progress(self.root,self.ensemble,1,network=False) as progress:
            progress('provider_finished',provider='sofascore',status='partial',source='partial_provider_schedule',current_fixtures=5)
            progress('needs_attention',delivery={'state':'queued','id':'bundle'})
        with self.reporter.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM queue WHERE kind='fixture'").fetchone()[0],1)
            self.assertGreater(db.execute("SELECT count(*) FROM queue WHERE kind='logs'").fetchone()[0],0)
        value=json.loads(path_for(self.root,'headless').read_text())
        self.assertEqual(value['state'],'needs_attention');self.assertEqual(value['partial_schedules'],1)
        self.assertEqual(value['baseline_fixtures'],5);self.assertEqual(value['complete_schedules'],0)

    def test_fixture_freshness_is_independent_and_main_receipt_confirms_delivery(self):
        fixture=self.fixture();stamp=datetime.now(timezone.utc)
        fixture.update(received_at=(stamp-timedelta(minutes=2)).isoformat(),delivery={'id':'bundle','state':'queued'})
        result=view(fixture,clock=stamp,receipt={'worker':'headless','id':'bundle','received_at':stamp.isoformat()})
        self.assertFalse(result['active']);self.assertEqual(result['connection'],'disconnected')
        self.assertEqual(result['delivery']['state'],'installed_on_main')
        fixture['received_at']=stamp.isoformat()
        self.assertTrue(view(fixture,clock=stamp)['active'])

    def test_fixture_milestones_appear_in_default_and_fixture_log_views(self):
        from collection.reporting import read_logs
        fixture=self.fixture()
        store=ReportingStore(self.base/'main',self.base/'main-ensemble')
        store.accept('headless',self.events())
        self.assertTrue(read_logs(self.base/'main',focus='important')['rows'])
        self.assertTrue(all(r['message'].startswith('[Fixtures]') for r in read_logs(self.base/'main',focus='fixtures')['rows']))

    def test_delayed_offline_heartbeat_does_not_revive_a_stopped_process(self):
        fixture=self.fixture();stamp=datetime.now(timezone.utc)
        fixture.update(received_at=stamp.isoformat(),heartbeat_at=(stamp-timedelta(days=1)).isoformat())
        self.assertFalse(view(fixture,clock=stamp)['active'])

    def test_live_fixture_heartbeat_and_terminal_event_reach_existing_main(self):
        import time
        from collection.reporting import ReportingServer
        server=ReportingServer(self.base/'main',self.base/'main-ensemble',
            {'bind':'127.0.0.1','port':0,'tokens':{'headless':'t'*40}}).start()
        self.addCleanup(server.close)
        atomic_json(self.root/'_system/config/reporting.json',{'mode':'client','worker_id':'headless',
            'url':'http://127.0.0.1:'+str(server.server.server_port),'token':'t'*40})
        path=self.base/'main-ensemble/reporting/runtime/headless.json'
        def wait_for(predicate):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if path.exists():
                    value=json.loads(path.read_text())
                    if predicate(value):return value
                time.sleep(.02)
            self.fail('Fixture heartbeat did not reach Main')
        with Progress(self.root,self.ensemble,1,heartbeat=.05) as progress:
            progress('season_started',competition='Europa League',season='2017-2018')
            progress('provider_started',provider='whoscored')
            progress('provider_progress',stage='Group L',month='201712')
            progress.publish()
            first=wait_for(lambda r:r.get('fixture_run',{}).get('stage')=='Group L')['fixture_run']
            second=wait_for(lambda r:r.get('fixture_run',{}).get('heartbeat_at','')>first['heartbeat_at'])['fixture_run']
            self.assertEqual(first['updated_at'],second['updated_at'])
            self.assertIsNone(progress.reporter.peer_thread)
            progress('finished',delivery={'state':'local_on_main'})
        final=wait_for(lambda r:r.get('fixture_run',{}).get('state')=='finished')
        self.assertEqual(final['state'],'fixture_only')
        self.assertNotIn('records',final)

    def test_reporting_cannot_replace_another_workers_fixture_state(self):
        fixture=self.fixture();fixture['worker_id']='msi'
        with self.assertRaises(ValueError):self.reporter.enqueue_fixture(fixture)

    def test_fixture_mode_does_not_download_inventories_or_reset_them(self):
        with patch.object(Reporter,'pull',side_effect=AssertionError('Fixture mode must not import inventories')):
            with Progress(self.root,self.ensemble,1,network=False):pass
        self.assertTrue(all('inventory' not in e and 'reset' not in e for e in self.events()))

    def test_registry_transfer_failure_does_not_stop_fixture_startup(self):
        from concordance.registry import Registry
        from concordance.sync import local_path,snapshot,install,snapshot_path
        from collection.reporting import ReportingDownloadError
        master=self.base/'main';registry=Registry(local_path(master));registry.assign('team',{'sofascore_id':'1'});registry.save()
        install(self.root,snapshot(master));before=snapshot_path(self.root).read_bytes()
        atomic_json(self.root/'_system/config/reporting.json',{'mode':'client','worker_id':'headless',
            'url':'http://127.0.0.1:8766','token':'t'*40})
        def start(reporter,*,snapshot=None):snapshot();return reporter
        with patch.object(Reporter,'_request_once',side_effect=ReportingDownloadError('Interrupted download')) as request,patch.object(Reporter,'start',autospec=True,side_effect=start):
            with Progress(self.root,self.ensemble,1) as progress:
                progress('season_started',competition='Premier League',season='2010-2011')
                self.assertEqual(progress.value['state'],'collecting')
        self.assertEqual(request.call_count,2)
        self.assertEqual(snapshot_path(self.root).read_bytes(),before)
        with self.reporter.connect() as db:
            messages=[json.loads(row[0]) for row in db.execute("SELECT payload FROM queue WHERE kind='logs'")]
        self.assertTrue(any('continuing with saved local IDs' in str(value) for value in messages))

    def test_interrupted_fixture_run_is_saved_without_touching_stats(self):
        stats={'worker_id':'headless','state':'waiting_for_extension'}
        path=self.root/'_system/workers/headless.json';atomic_json(path,stats)
        with self.assertRaises(KeyboardInterrupt):
            with Progress(self.root,self.ensemble,1,network=False):raise KeyboardInterrupt()
        self.assertEqual(json.loads(path.read_text()),stats)
        self.assertEqual(json.loads(path_for(self.root,'headless').read_text())['state'],'stopped')

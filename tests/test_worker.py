from tests.paths import system_path
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import yaml
import config
from collection.worker import run, RESTART
from collection.progress import CollectionProgress, observe_progress
from collection.workerRuntime import Runtime


class NoSync:
    def __init__(self,*args):self.status={'state':'disabled'};self.calls=0
    def start(self):return self
    def close(self):pass
    def boundary(self):self.calls+=1;return []


class WorkerTests(unittest.TestCase):
    def test_main_done_overrides_old_pause_without_scraping_and_publishes_completed_seasons(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from config.workerPlan import load_plan
        from collection.workInstructions import install,FORMAT
        from collection.storageAgent import season_for
        from storage.pandasStore import atomic_json
        self.document['storage']['enabled']=True;self.save();plan=load_plan(self.plan)
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        jobs=plan.for_worker('main');commands={j.id:{'done':True,'configuration':'reviewed-config','closed':True,'counts':{},'waivers':{}} for j in jobs}
        install(self.root,{'format':FORMAT,'worker_id':'main','plan_revision':plan.revision,'jobs':commands,'approved':{}})
        atomic_json(system_path(self.root,'worker_runtime')/'main.json',{'jobs':{j.id:{'state':'paused','exit_code':2} for j in jobs}})
        for job in jobs:atomic_json(self.root/season_for(job)/'job.json',{'competition':job.competition})
        reporter=MagicMock();reporter.start.return_value=reporter;reporter.status={'state':'connected'}
        storage=MagicMock();storage.start.return_value=storage;storage.status={'state':'listening'}
        control=MagicMock();control.status={'state':'granted'}
        calls=[]
        with patch('collection.worker.load_local',return_value={'mode':'client'}), \
             patch('collection.worker.Reporter',return_value=reporter), \
             patch('collection.jobControl.AssignmentClient',return_value=control), \
             patch('collection.storageAgent.StorageAgent',return_value=storage), \
             patch('collection.worker.prepare_extension',return_value=SimpleNamespace(ready=False)):
            self.assertEqual(run(self.args,runner=lambda args:calls.append(args),session=self.session,sync_factory=NoSync),0)
        self.assertEqual(calls,[]);self.assertEqual(storage.finished.call_count,len(jobs))
        runtime=json.loads((system_path(self.root,'worker_runtime')/'main.json').read_text())
        self.assertEqual(runtime['state'],'done');self.assertTrue(all(r['exit_code']==0 for r in runtime['jobs'].values()))

    def setUp(self):
        extension=patch('collection.worker.prepare_extension',return_value=None);extension.start();self.addCleanup(extension.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.ensemble=self.base/'ensemble'
        self.plan=self.base/'workers.yaml'
        self.document=yaml.safe_load((config.PROJECT_ROOT/'config/workers.yaml').read_text())
        self.document['dashboard']['enabled']=False;self.document['git']['enabled']=False
        self.document['storage']['enabled']=False
        from copy import deepcopy
        self.document['workers']['main']['assignments']=deepcopy(self.document['workers']['msi']['assignments'])
        self.document['workers']['msi']['assignments']=[]
        self.document['workers']['main']['role']='worker'
        self.document['workers']['main']['assignments'][0]['competitions']=['La Liga','Bundesliga']
        self.save();self.args=['--plan',str(self.plan),'--worker-id','main','--data-root',str(self.root),
                              '--ensemble-root',str(self.ensemble),'--once','--no-dashboard']
        self.sessions=0

    def save(self):self.plan.write_text(yaml.safe_dump(self.document))

    @contextmanager
    def session(self,*args,**kwargs):
        self.sessions+=1
        class Progress:
            path=self.ensemble/'provenance.json'
            def save(self):pass
        yield Progress()

    def test_supervised_worker_without_main_does_not_scrape(self):
        self.document['storage']['enabled']=True;self.save()
        with patch('collection.worker.load_local',return_value=None):
            calls=[]
            self.assertEqual(run(self.args,runner=lambda args:calls.append(args) or 0,session=self.session,sync_factory=NoSync),2)
            self.assertEqual(calls,[])
            runtime=json.loads((system_path(self.root,'worker_runtime')/'main.json').read_text())
            self.assertEqual(runtime['state'],'waiting_for_instructions')

    def test_waits_for_extension_before_scraping_and_idle_identity_lookups(self):
        from types import SimpleNamespace
        bridge=SimpleNamespace(ready=False);stop=threading.Event();calls=[]
        def wait(seconds):
            self.assertEqual(calls,[]);self.assertEqual(self.sessions,1)
            bridge.ready=True
            return False
        with patch('collection.worker.prepare_extension',return_value=bridge),patch.object(stop,'wait',side_effect=wait),patch('providers.playerProfiles.investigate_queue') as profiles:
            self.assertEqual(run(self.args,runner=lambda args:calls.append(args) or 0,session=self.session,sync_factory=NoSync,stop=stop),0)
            self.assertEqual(len(calls),2);profiles.assert_not_called()

    def test_reconnect_retries_interrupted_season_from_checkpoints(self):
        from types import SimpleNamespace
        bridge=SimpleNamespace(ready=True);stop=threading.Event();calls=[]
        self.document['workers']['main']['assignments'][0]['competitions']=['La Liga'];self.save()
        def runner(args):
            calls.append(args)
            if len(calls)==1:bridge.ready=False;return 2
            return 0
        def wait(seconds):bridge.ready=True;return False
        with patch('collection.worker.prepare_extension',return_value=bridge),patch.object(stop,'wait',side_effect=wait):
            self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync,stop=stop),0)
        self.assertEqual(len(calls),2);self.assertEqual(calls[0],calls[1])

    def test_runs_only_assigned_jobs_single_inventory_session_and_resumes_finished_plan(self):
        jobs=[]
        def runner(args):jobs.append(args);return 0
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),2);self.assertEqual(self.sessions,1)
        self.assertEqual(json.loads((system_path(self.root,"identity.json")).read_text())['worker_id'],'main')
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),2);self.assertEqual(self.sessions,1)
        self.assertEqual(run(self.args+['--retry'],runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),4)

    def test_unassigned_msi_never_initializes_collectors_and_remembers_identity(self):
        args=[v if v!='main' else 'msi' for v in self.args]
        def no_job(args):raise AssertionError('Unassigned worker scraped')
        self.assertEqual(run(args,runner=no_job,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(self.sessions,0)
        self.assertEqual(json.loads((system_path(self.root,"identity.json")).read_text())['worker_id'],'msi')

    def test_local_plan_changes_restart_only_after_runner_returns(self):
        calls=[]
        def runner(args):
            calls.append(args);self.document['workers']['main']['assignments']=[];self.save();return 0
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),RESTART)
        self.assertEqual(len(calls),1)

    def test_paused_jobs_return_failure_and_wait_for_explicit_retry(self):
        calls=[]
        def runner(args):calls.append(args);return 2
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),2)
        self.assertEqual(len(calls),2)
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),2)
        self.assertEqual(len(calls),2)

    def test_changed_plan_during_inventory_is_rechecked_before_any_scrape(self):
        calls=[]
        @contextmanager
        def auditing(*args,**kwargs):
            self.document['workers']['main']['assignments']=[];self.save()
            with self.session(*args,**kwargs) as progress:yield progress
        code=run(self.args,runner=lambda args:calls.append(args) or 0,session=auditing,sync_factory=NoSync)
        self.assertEqual(code,RESTART);self.assertEqual(calls,[])

    def test_duplicate_id_does_not_rewrite_existing_local_identity(self):
        from storage.pandasStore import atomic_json
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        identity={'worker_id':'original','instance_id':'original-folder'}
        atomic_json(system_path(self.root,"identity.json"),identity)
        atomic_json(self.ensemble/'workers/main.json',{'format':'soccer-analysis-progress-v1',
            'worker_id':'main','instance_id':'different-folder','records':{}})
        with self.assertRaisesRegex(ValueError,'another data folder'):
            run(self.args,runner=lambda args:0,session=self.session,sync_factory=NoSync)
        self.assertEqual(json.loads((system_path(self.root,"identity.json")).read_text()),identity)

    def test_unknown_worker_fails_without_writing(self):
        with self.assertRaises(ValueError):run([v if v!='main' else 'unknown' for v in self.args],sync_factory=NoSync)
        self.assertFalse(self.root.exists())

    def test_progress_hook_reports_schedule_match_and_result_without_paths(self):
        events=[];job=CollectionProgress(enabled=False).job('/not-for-publication','whoscored_events','La Liga','2023-2024')
        with observe_progress(events.append):
            job.begin();job.schedule([{'id':123}]);job.start_game(123);job.finish_game();job.end()
        self.assertEqual([e['event'] for e in events],['schedule','match_started','match_saved','job_finished'])
        self.assertNotIn('/not-for-publication',json.dumps(events))


    def test_idle_status_does_not_create_repeated_publish_changes(self):
        runtime=Runtime(self.root,self.ensemble,'main')
        runtime.save(state='idle',git={'state':'current'})
        before=runtime.public.read_bytes()
        runtime.save(state='idle',git={'state':'current'})
        self.assertEqual(before,runtime.public.read_bytes())
        self.assertNotIn(str(self.root),runtime.public.read_text())

    def test_direct_publication_queues_every_saved_match_and_never_uses_git_to_publish(self):
        self.document['workers']['main']['assignments'][0]['competitions']=['La Liga'];self.save()
        time=[0];queued=[]
        class Direct:
            status={'state':'unconfigured'}
            def __init__(self,*args):pass
            def start(self):return self
            def close(self):pass
            def enqueue(self,**kwargs):queued.append(time[0])
            def log(self,*args):pass
        class Sync(NoSync):
            def publish(self):raise AssertionError('Git must never publish worker progress')
        def runner(args):
            job=CollectionProgress(enabled=False,clock=lambda:time[0]).job('/private/path','combined','La Liga','2021-2022')
            job.begin();job.schedule([{'id':1},{'id':2}]);job.start_game(1)
            time[0]=1;job.finish_game();self.assertIn(1,queued)
            job.start_game(2);time[0]=2;job.finish_game();self.assertIn(2,queued)
            time[0]=3;job.end();return 0
        with patch('collection.worker.Reporter',Direct):
            self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=Sync,clock=lambda:time[0]),0)
        state=json.loads((self.root/'_system/workers/main.json').read_text())
        self.assertEqual(next(iter(state['schedules'].values()))['match_ids'],['1','2'])
        phase=next(iter(state['phase_stats'].values()))
        self.assertEqual(phase['scraped'],2);self.assertIn('eta_seconds',phase)

    def test_monitor_never_scrapes_even_with_assignments(self):
        calls=[]
        self.assertEqual(run(self.args+['--monitor'],runner=lambda args:calls.append(args) or 0,
            session=self.session,sync_factory=NoSync),0)
        self.assertEqual(calls,[]);self.assertEqual(self.sessions,0)
        state=json.loads((self.root/'_system/workers/main.json').read_text())
        self.assertEqual(state['role'],'monitor');self.assertEqual(state['state'],'monitoring')

    def test_runtime_retains_targets_and_finished_job_timing_across_restart(self):
        state=Runtime(self.root,self.ensemble,'main')
        state.save(schedules={'one':{'match_ids':['1','2']}},job={'id':'one'},
            job_started_at='2026-10-19T10:00:00+00:00',job_ended_at='2026-10-19T11:00:00+00:00')
        restarted=Runtime(self.root,self.ensemble,'main')
        self.assertEqual(restarted.value['schedules']['one']['match_ids'],['1','2'])
        self.assertEqual(restarted.value['job_ended_at'],'2026-10-19T11:00:00+00:00')

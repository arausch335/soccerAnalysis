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
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.ensemble=self.base/'ensemble'
        self.plan=self.base/'workers.yaml'
        self.document=yaml.safe_load((config.PROJECT_ROOT/'config/workers.yaml').read_text())
        self.document['dashboard']['enabled']=False;self.document['git']['enabled']=False
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

    def test_runs_only_assigned_jobs_single_inventory_session_and_resumes_finished_plan(self):
        jobs=[]
        def runner(args):jobs.append(args);return 0
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),2);self.assertEqual(self.sessions,1)
        self.assertEqual(json.loads((self.root/'identity.json').read_text())['worker_id'],'main')
        self.assertEqual(run(self.args,runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),2);self.assertEqual(self.sessions,1)
        self.assertEqual(run(self.args+['--retry'],runner=runner,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(len(jobs),4)

    def test_unassigned_msi_never_initializes_collectors_and_remembers_identity(self):
        args=[v if v!='main' else 'msi' for v in self.args]
        def no_job(args):raise AssertionError('Unassigned worker scraped')
        self.assertEqual(run(args,runner=no_job,session=self.session,sync_factory=NoSync),0)
        self.assertEqual(self.sessions,0)
        self.assertEqual(json.loads((self.root/'identity.json').read_text())['worker_id'],'msi')

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
        atomic_json(self.root/'identity.json',identity)
        atomic_json(self.ensemble/'workers/main.json',{'format':'soccer-analysis-progress-v1',
            'worker_id':'main','instance_id':'different-folder','records':{}})
        with self.assertRaisesRegex(ValueError,'another data folder'):
            run(self.args,runner=lambda args:0,session=self.session,sync_factory=NoSync)
        self.assertEqual(json.loads((self.root/'identity.json').read_text()),identity)

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

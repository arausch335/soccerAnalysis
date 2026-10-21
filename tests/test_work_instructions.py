from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import config
from config.workerPlan import load_plan
from collection.workInstructions import compile_instructions,install
from collection.ensembleProgress import PortableProgress,job_key,remote_done,progress_session
from collection.workerIdentity import ensure_identity
from storage.pandasStore import atomic_json


class InstructionsTests(unittest.TestCase):
    def setUp(self):
        self.plan=load_plan(config.PROJECT_ROOT/'config/workers.yaml');self.job=self.plan.for_worker('msi')[0]
        self.season=self.job.as_dict()['season']
        self.key=job_key('sofascore',self.job.competition,self.season,1)
        self.who_key=job_key('whoscored_events',self.job.competition,self.season,9001)
        self.required=['provider:sofascore','provider:fotmob','provider:whoscored','sofascore_match_stats','spatial:average_positions']
        row={'namespace':'sofascore','competition':self.job.competition,'season':self.season,'match_id':'1',
             'status':'complete','capabilities':self.required,'holders':['headless'],'origins':['headless'],
             'provider_match_ids':{'whoscored':'9001'},
             'resources':{name:'complete' for name in self.job.resources if name!='whoscored_events'}}
        events={'namespace':'whoscored_events','competition':self.job.competition,'season':self.season,'match_id':'9001',
                'status':'complete','capabilities':[],'holders':['msi'],'origins':['headless'],
                'resources':{'whoscored_events':'complete'}}
        self.reports=[{'inventory':{'records':{self.key:row,self.who_key:events}},'runtime':{'schedules':{
            'one':{'competition':self.job.competition,'season':self.season,'kind':'combined','match_ids':[1,2]}}}}]

    def test_main_combines_provider_evidence_and_tells_worker_what_remains(self):
        value=compile_instructions(self.plan,'msi',self.reports)
        self.assertIn(self.key,value['approved'])
        job=value['jobs'][self.job.id]
        self.assertTrue(job['schedule_known']);self.assertEqual(job['known_matches'],2)
        self.assertEqual(set(job['remaining']),{'2'})
        self.assertEqual(job['remaining_matches'],1)
        # A later partial holder cannot erase another holder's verified resource.
        partial=deepcopy(self.reports[0]['inventory']['records'][self.key]);partial['resources']={'sofascore_stats':'partial'}
        value=compile_instructions(self.plan,'msi',self.reports+[{'inventory':{'records':{self.key:partial}}}])
        self.assertIn(self.key,value['approved'])

    def test_local_inventory_alone_cannot_authorize_a_supervised_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'data';atomic_json(root/'.soccer-scraper.json',{'format':1});ensure_identity(root,'msi')
            progress=PortableProgress(Path(temporary)/'ensemble/provenance.json',root,worker_id='msi')
            progress.trusted_only=True;progress.plan_revision=self.plan.revision
            progress.records={self.key:self.reports[0]['inventory']['records'][self.key]}
            with progress_session(progress):
                self.assertFalse(remote_done('sofascore',self.job.competition,self.season,1,self.required))
                install(root,compile_instructions(self.plan,'msi',self.reports),worker='msi',revision=self.plan.revision)
                self.assertTrue(remote_done('sofascore',self.job.competition,self.season,1,self.required))
                # Retraction on Main takes effect on the next instructions update.
                install(root,compile_instructions(self.plan,'msi',[]),worker='msi',revision=self.plan.revision)
                self.assertFalse(remote_done('sofascore',self.job.competition,self.season,1,self.required))

    def test_unknown_schedule_never_claims_there_is_nothing_to_discover(self):
        value=compile_instructions(self.plan,'msi',[])
        self.assertFalse(value['jobs'][self.job.id]['schedule_known'])
        self.assertFalse(value['approved'])
        self.assertEqual(set(value['jobs']),{job.id for job in self.plan.for_worker('msi')})

    def test_wrong_worker_or_plan_instructions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            value=compile_instructions(self.plan,'msi',self.reports)
            with self.assertRaises(ValueError):install(temporary,value,worker='headless')
            with self.assertRaises(ValueError):install(temporary,value,revision='old')

    def test_main_can_reopen_finished_work_after_inventory_changes_without_a_retry_loop(self):
        from collection.workInstructions import requested_again
        self.reports[0]['runtime'].update(worker_id='msi',jobs={self.job.id:{'state':'finished'}})
        value=compile_instructions(self.plan,'msi',self.reports)
        command=value['jobs'][self.job.id]
        self.assertTrue(command['recheck'])
        with tempfile.TemporaryDirectory() as temporary:
            install(temporary,value,worker='msi',revision=self.plan.revision)
            self.assertIn(self.job.id,requested_again(temporary,'msi',self.plan.revision,{}))
            attempted={self.job.id:{'instruction_revision':command['revision']}}
            self.assertNotIn(self.job.id,requested_again(temporary,'msi',self.plan.revision,attempted))
            self.reports[0]['runtime']['jobs']=attempted
            refreshed=compile_instructions(self.plan,'msi',self.reports)
            self.assertFalse(refreshed['jobs'][self.job.id]['recheck'])

    def test_imported_inventory_reports_targets_even_when_no_scraper_is_running(self):
        self.reports[0]['inventory']['schedules']=self.reports[0]['runtime']['schedules']
        self.reports[0]['runtime']={}
        value=compile_instructions(self.plan,'msi',self.reports)
        command=value['jobs'][self.job.id]
        self.assertTrue(command['schedule_known']);self.assertEqual(command['remaining_matches'],1)


if __name__=='__main__':unittest.main()

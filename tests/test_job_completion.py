from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from collection.jobCompletion import CompletionStore,configuration
from collection.workInstructions import compile_instructions
from collection.ensembleProgress import job_key
from config.workerPlan import load_plan


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.plan=load_plan();self.job=self.plan.for_worker('msi')[0];self.season=self.job.as_dict()['season']
        self.key=job_key('sofascore',self.job.competition,self.season,1)
        self.event_key=job_key('whoscored_events',self.job.competition,self.season,901)
        self.row={'namespace':'sofascore','competition':self.job.competition,'season':self.season,'match_id':'1',
            'status':'complete','capabilities':['provider:sofascore','provider:fotmob','provider:whoscored','sofascore_match_stats'],
            'holders':['msi'],'origins':['msi'],'resources':{r:'complete' for r in self.job.resources if r!='whoscored_events'},
            'provider_match_ids':{'whoscored':'901'},'evidence':{'attempts':2,'label':'A vs B','error':'matchCentreData not available'}}
        self.schedule={'competition':self.job.competition,'season':self.season,'kind':'combined','match_ids':['1'],'scope_version':2}
        self.reports=[{'inventory':{'records':{self.key:self.row,self.event_key:{'namespace':'whoscored_events','competition':self.job.competition,
            'season':self.season,'match_id':'901','status':'complete','resources':{'whoscored_events':'complete'},'holders':['msi'],'origins':['msi']}},
            'schedules':{'one':self.schedule}},'runtime':{'worker_id':'msi','jobs':{self.job.id:{'state':'paused'}}}}]
    def summary(self,store=None):
        return compile_instructions(self.plan,'msi',self.reports,store.snapshot() if store else None)['jobs'][self.job.id]
    def test_fast_done_ignores_maps_all_reopens_for_its_own_requirements(self):
        fast=self.summary();self.assertTrue(fast['done']);self.assertNotIn('average_positions',fast['counts'])
        self.assertEqual(fast['groups'][0]['percent'],100)
        self.plan.jobs=[replace(j,spatial_capture='all') if j.id==self.job.id else j for j in self.plan.jobs]
        full=self.summary();self.assertFalse(full['done']);self.assertEqual(set(full['remaining']['1']),{'average_positions','heatmaps','shotmaps','action_maps'})
        self.assertNotEqual(full['configuration'],fast['configuration'])
    def test_unavailable_requires_reasoned_review_and_closure_survives_restart(self):
        self.row['resources']['whoscored_stats']='unavailable'
        self.assertFalse(self.summary()['done']);self.assertEqual(len(self.summary()['review']),1)
        with tempfile.TemporaryDirectory() as root:
            store=CompletionStore(root)
            with self.assertRaises(ValueError):store.record(self.job,'unavailable',mid='1',resources=['whoscored_stats'])
            store.record(self.job,'unavailable',mid='1',resources=['whoscored_stats'],reason='Reviewed missing matchCentreData')
            summary=self.summary(store);self.assertTrue(summary['done']);self.assertFalse(summary['closed'])
            self.assertEqual(summary['counts']['whoscored_stats'],{'complete':0,'unavailable':1})
            store.record(self.job,'close',reason='Required data collected or reviewed unavailable')
            self.assertTrue(self.summary(CompletionStore(root))['closed'])
            self.assertEqual(configuration(self.job),configuration(replace(self.job,worker='headless',id='moved')))
            # New fixtures and lost evidence reopen work despite a declaration.
            self.schedule['match_ids'].append('2');self.assertFalse(self.summary(store)['done'])
            self.schedule['match_ids'].pop();self.row['resources']['sofascore_stats']='failed'
            self.assertFalse(self.summary(store)['done'])
    def test_reviews_can_be_retried_and_reopened_without_fabricating_collected_data(self):
        self.row['resources']['whoscored_stats']='failed'
        with tempfile.TemporaryDirectory() as root:
            store=CompletionStore(root);store.record(self.job,'unavailable',mid='1',resources=['whoscored_stats'],reason='No match data published',attempts=2)
            store.record(self.job,'close',reason='Data availability reviewed')
            store.record(self.job,'reopen',reason='Provider may have repaired data')
            self.assertFalse(self.summary(store)['done']);self.assertFalse(self.summary(store)['closed'])
            store.record(self.job,'retry',mid='1',resources=['whoscored_stats'],attempts=2)
            self.assertEqual(self.summary(store)['review'],[])
            self.row['evidence']['attempts']=3
            self.assertEqual(len(self.summary(store)['review']),1)
            self.assertGreaterEqual(len(store.snapshot()['history']),4)
    def test_scoped_inventory_beats_older_mixed_runtime_schedule(self):
        self.reports[0]['runtime']['schedules']={'old':{**self.schedule,'scope_version':1,'match_ids':['1','999']}}
        self.assertEqual(self.summary()['known_matches'],1)
    def test_stats_do_not_satisfy_events_and_failures_without_checkpoints_are_reviewable(self):
        del self.reports[0]['inventory']['records'][self.event_key]
        self.assertEqual(self.summary()['remaining']['1'],['whoscored_events'])
        self.assertFalse(self.summary()['done'])
        self.row['resources']={r:'failed' for r in self.job.resources};self.row['capabilities']=[];self.row['status']='failed'
        self.assertEqual(self.summary()['review'][0]['label'],'A vs B')
    def test_configuration_change_does_not_reuse_waivers(self):
        self.row['resources']['whoscored_stats']='failed'
        with tempfile.TemporaryDirectory() as root:
            store=CompletionStore(root);store.record(self.job,'unavailable',mid='1',resources=['whoscored_stats'],reason='Confirmed missing data')
            self.assertTrue(self.summary(store)['done'])
            self.plan.jobs=[replace(j,spatial_capture='all') if j.id==self.job.id else j for j in self.plan.jobs]
            summary=self.summary(store)
            self.assertIn('whoscored_stats',summary['remaining']['1']);self.assertFalse(summary['done'])


if __name__=='__main__':unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from config.workerPlan import load_plan
from dashboard.server import start_server
from storage.pandasStore import atomic_json
from tests import test_job_completion as fixtures


class CollectionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.ensemble=self.base/'ensemble'
        fixture=fixtures.CompletionTests();fixture.setUp();self.job=fixture.job
        fixture.row['resources']['whoscored_stats']='failed'
        saved={'format':'soccer-analysis-progress-v1','worker_id':'msi',**fixture.reports[0]['inventory']}
        atomic_json(self.ensemble/'reporting/workers/msi.json',saved)
        self.server=start_server(self.root,self.ensemble,load_plan().path,'main',0);self.addCleanup(lambda:self.server.close())
        self.url=self.server.url
    def read(self):
        with urlopen(self.url+'/api/collection-review') as r:return json.load(r)
    def post(self,value,token=None,origin=True):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Review-Token']=token
        if origin:headers['Origin']=self.url
        with urlopen(Request(self.url+'/api/collection-review',data=json.dumps(value).encode(),headers=headers)) as r:return json.load(r)
    def test_review_close_and_reopen_are_persistent_main_only_actions(self):
        review=self.read();job=next(j for j in review['jobs'] if j['id']==self.job.id)
        self.assertFalse(job['done']);self.assertEqual(job['groups'][0]['percent'],200/3)
        value={'job_id':self.job.id,'configuration':job['configuration'],'action':'unavailable',
               'match_id':'1','resources':['whoscored_stats'],'reason':'Verified no published matchCentreData'}
        with self.assertRaises(HTTPError) as failure:self.post(value)
        self.assertEqual(failure.exception.code,403)
        self.post(value,review['review_token'])
        job=next(j for j in self.read()['jobs'] if j['id']==self.job.id)
        self.assertTrue(job['done']);self.assertFalse(job['closed'])
        # Reviewed data is not misrepresented as downloaded data.
        self.assertEqual(job['groups'][0]['percent'],200/3)
        self.assertEqual(job['groups'][0]['resolved_percent'],100)
        self.post({'job_id':self.job.id,'configuration':job['configuration'],'action':'close','reason':'Season fully accounted for'},review['review_token'])
        self.assertTrue(next(j for j in self.read()['jobs'] if j['id']==self.job.id)['closed'])
        self.post({'job_id':self.job.id,'configuration':job['configuration'],'action':'reopen','reason':'Check provider again'},review['review_token'])
        self.assertFalse(next(j for j in self.read()['jobs'] if j['id']==self.job.id)['done'])
    def test_confirmation_survives_dashboard_restart_and_returns_named_history(self):
        review=self.read();job=next(j for j in review['jobs'] if j['id']==self.job.id)
        self.post({'job_id':self.job.id,'configuration':job['configuration'],'action':'unavailable','match_id':'1',
            'resources':['whoscored_stats'],'reason':'Verified source data unavailable'},review['review_token'])
        self.server.close()
        self.server=start_server(self.root,self.ensemble,load_plan().path,'main',0)
        self.url=self.server.url
        restored=self.read();job=next(j for j in restored['jobs'] if j['id']==self.job.id)
        self.assertTrue(job['done']);self.assertTrue(job['has_review_history']);self.assertEqual(job['waivers'],{'1':['whoscored_stats']})
        self.assertEqual(restored['history'][0]['competition'],self.job.competition)
        self.assertEqual(restored['history'][0]['reason'],'Verified source data unavailable')

    def test_review_fixture_date_and_names_are_backfilled_from_season_schedule(self):
        atomic_json(self.root/'premier-league'/self.job.as_dict()['season']/'schedule.json',{'events':[
            {'id':1,'homeTeam':{'name':'Tottenham'},'awayTeam':{'name':'Chelsea'},'startTimestamp':1632065400}]})
        job=next(j for j in self.read()['jobs'] if j['id']==self.job.id)
        self.assertEqual(job['review'][0]['date'],'2021-09-19')
        self.assertEqual(job['review'][0]['label'],'A vs B')  # Existing inventory names win.

    def test_cannot_close_unresolved_season_or_apply_stale_configuration(self):
        review=self.read();job=next(j for j in review['jobs'] if j['id']==self.job.id)
        for value in ({'job_id':self.job.id,'configuration':job['configuration'],'action':'close','reason':'Skip without reviewing'},
                      {'job_id':self.job.id,'configuration':'old','action':'unavailable','match_id':'1','resources':['whoscored_stats'],'reason':'Missing source data'}):
            with self.assertRaises(HTTPError) as failure:self.post(value,review['review_token'])
            self.assertEqual(failure.exception.code,409)


if __name__=='__main__':unittest.main()

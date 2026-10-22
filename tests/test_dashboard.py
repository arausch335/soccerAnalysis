import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import yaml
import config
from dashboard.model import DashboardModel, resource_states
from dashboard.server import start_server
from storage.pandasStore import atomic_json


class DashboardTests(unittest.TestCase):
    def test_legacy_mixed_schedule_has_separate_main_and_qualifier_targets(self):
        atomic_json(self.root/'champions-league/2023-2024/schedule.json',{'events':[
            {'id':1,'tournament':{}},{'id':2,'tournament':{'qualificationOrPreliminary':True}}]})
        model=DashboardModel(self.root,self.ensemble,self.plan,'main');warnings=[]
        self.assertEqual(model._schedule('Champions League','2023-2024','sofascore',warnings),{'1'})
        self.assertEqual(model._schedule('Champions League Qualifiers','2023-2024','sofascore',warnings),{'2'})
        self.assertIsNone(model._schedule('Champions League Qualifiers','2023-2024','whoscored_events',warnings))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.ensemble=self.base/'ensemble_data'
        self.plan=self.base/'workers.yaml'
        document=yaml.safe_load((config.PROJECT_ROOT/'config/workers.yaml').read_text())
        from copy import deepcopy
        document['workers']['main']['assignments']=deepcopy(document['workers']['msi']['assignments'])
        document['workers']['main']['assignments'][0]['seasons']=['2023/24']
        document['workers']['msi']['assignments']=[]
        self.plan.write_text(yaml.safe_dump(document))

    def inventory(self,worker,rows):
        atomic_json(self.ensemble/'workers'/(worker+'.json'),{'format':'soccer-analysis-progress-v1','worker_id':worker,'records':rows})

    def row(self,resource=None,namespace='sofascore'):
        return {'namespace':namespace,'competition':'La Liga','season':'2023-2024','match_id':'1',
                'status':'complete','capabilities':['sofascore_match_stats','spatial:average_positions'],
                'origins':['msi'],'holders':['main'],'updated_at':'2026-10-19T10:00:00+00:00',
                **({'resources':resource} if resource is not None else {})}

    def test_unknown_spatial_is_not_reported_as_success_and_native_events_are_saved(self):
        self.assertEqual(resource_states(self.row())['average_positions'],'unknown')
        self.assertEqual(resource_states(self.row({'average_positions':'unavailable'}))['average_positions'],'unavailable')
        self.assertEqual(resource_states(self.row(namespace='whoscored_events'))['whoscored_events'],'complete')
        self.assertEqual(resource_states(self.row({'sofascore_stats':'complete'}))['fotmob_enrichment'],'not_attempted')
        self.assertEqual(resource_states(self.row())['fotmob_enrichment'],'pending')

    def test_deduplicated_total_preserves_each_holder_and_collector(self):
        row=self.row();self.inventory('main',{'one':row});self.inventory('msi',{'one':row})
        model=DashboardModel(self.root,self.ensemble,self.plan,'main');status=model.status()
        self.assertEqual(status['unique_records'],1)
        self.assertEqual(sum(w['records'] for w in status['workers']),2)
        self.assertEqual(next(w['collected'] for w in status['workers'] if w['id']=='main'),0)
        result=model.matches({'holder':['main'],'resource':['average_positions']})
        self.assertEqual(result['total'],1);self.assertEqual(result['rows'][0]['origins'],['msi'])

    def test_saved_schedule_targets_and_planned_missing_data(self):
        self.inventory('main',{'one':self.row({'average_positions':'unavailable'})})
        atomic_json(self.root/'la-liga/2023-2024/schedule.json',{'events':[{'id':1},{'id':2}]})
        model=DashboardModel(self.root,self.ensemble,self.plan,'main');status=model.status()
        row=next(c for c in status['coverage'] if c['worker']=='main' and c['competition']=='La Liga' and c['season']=='2023-2024' and c['resource']=='average_positions')
        self.assertEqual(row['total'],2);self.assertEqual(row['counts'],{'unavailable':1,'missing':1});self.assertFalse(row['planned'])
        required=next(c for c in status['coverage'] if c['worker']=='main' and c['competition']=='La Liga' and c['season']=='2023-2024' and c['resource']=='sofascore_stats')
        self.assertTrue(required['planned'])
        unknown=next(c for c in status['coverage'] if c['competition']=='Premier League')
        self.assertIsNone(unknown['total'])

    def test_remote_activity_is_labelled_published_and_invalid_inventory_warns(self):
        self.inventory('msi',{})
        atomic_json(self.ensemble/'runtime/msi.json',{'format':'soccer-worker-runtime-v1','worker_id':'msi','state':'scraping'})
        (self.ensemble/'workers/main.json').write_text('invalid')
        status=DashboardModel(self.root,self.ensemble,self.plan,'main').status()
        self.assertTrue(status['warnings'])
        self.assertEqual(next(w for w in status['workers'] if w['id']=='msi')['runtime_source'],'published')

    def test_server_is_read_only_and_rejects_foreign_hosts_and_paths(self):
        server=start_server(self.root,self.ensemble,self.plan,'main',0);self.addCleanup(server.close)
        with urlopen(server.url+'/api/status') as response:
            self.assertEqual(response.status,200);self.assertIn('workers',json.load(response))
            self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        for suffix,method,host,code in [('/../config/settings.yaml','GET',None,404),
             ('/api/status','POST',None,403),('/api/status','GET','attacker.invalid',403)]:
            request=Request(server.url+suffix,method=method,headers={'Host':host} if host else {})
            with self.subTest(suffix=suffix,method=method),self.assertRaises(HTTPError) as error:urlopen(request)
            self.assertEqual(error.exception.code,code)


    def test_received_runtime_uses_connection_age_and_logs_endpoint_supports_filters(self):
        from datetime import datetime, timezone
        from collection.reporting import ReportingStore
        store=ReportingStore(self.root,self.ensemble)
        store.accept('msi',[{'instance_id':'folder','stream':'stream','sequence':1,
            'runtime':{'format':'soccer-worker-runtime-v1','worker_id':'msi','state':'scraping','updated_at':datetime.now(timezone.utc).isoformat()},
            'logs':[{'at':'2026-10-19T12:00:00+00:00','level':'ERROR','message':'WhoScored match 22 failed'}]}])
        model=DashboardModel(self.root,self.ensemble,self.plan,'main')
        info=next(w for w in model.status()['workers'] if w['id']=='msi')
        self.assertEqual(info['connection'],'connected');self.assertEqual(info['runtime_source'],'direct')
        server=start_server(self.root,self.ensemble,self.plan,'main',0);self.addCleanup(server.close)
        with urlopen(server.url+'/api/logs?worker=msi&level=ERROR&search=WhoScored') as response:
            rows=json.load(response)['rows']
        self.assertEqual(rows[0]['message'],'WhoScored match 22 failed')
        saved=self.ensemble/'reporting/runtime/msi.json';runtime=json.loads(saved.read_text());runtime['received_at']='2020-01-01T00:00:00+00:00';atomic_json(saved,runtime)
        model=DashboardModel(self.root,self.ensemble,self.plan,'main')
        self.assertEqual(next(w for w in model.status()['workers'] if w['id']=='msi')['connection'],'disconnected')

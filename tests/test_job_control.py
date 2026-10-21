from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
import config
from config.workerPlan import load_plan
from collection.jobControl import JobControl,AssignmentClient,receipt_path
from collection.reporting import Reporter,ReportingServer
from collection.workerIdentity import ensure_identity


class ControlTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.base=Path(temporary.name)
        self.path=self.base/'workers.yaml';self.document=yaml.safe_load((config.PROJECT_ROOT/'config/workers.yaml').read_text())
        self.save();self.control=JobControl(self.base/'main',self.path);self.job=self.plan.for_worker('msi')[0]
    def save(self):self.path.write_text(yaml.safe_dump(self.document));self.plan=load_plan(self.path)
    def claim(self,worker='msi',instance='original',job=None,revision=None,token='x'*40):
        return self.control.command(worker,{'operation':'claim','job_id':(job or self.job).id,'revision':revision or self.plan.revision,'instance':instance,'token':token})

    def test_only_current_main_plan_assignments_can_start(self):
        self.assertEqual(self.claim(revision='old')['state'],'waiting')
        self.assertEqual(self.claim(worker='headless')['state'],'waiting')
        self.assertEqual(self.claim()['state'],'granted')
        self.assertEqual(self.claim()['state'],'granted')
        self.assertEqual(self.claim(instance='other')['state'],'waiting')
        self.assertEqual(self.claim(token='new-run-'+'z'*40)['state'],'waiting')

    def test_plan_handoff_waits_for_old_worker_to_release_and_survives_restart(self):
        grant=self.claim();self.assertEqual(grant['state'],'granted')
        item=deepcopy(self.document['workers']['msi']['assignments'][0]);item['competitions']=[self.job.competition];item['id']='handoff'
        self.document['workers']['msi']['assignments']=[];self.document['workers']['headless']['assignments'].append(item);self.save()
        replacement=next(j for j in self.plan.for_worker('headless') if j.competition==self.job.competition and j.season==self.job.season)
        self.control=JobControl(self.base/'main',self.path)
        self.assertEqual(self.claim(worker='headless',job=replacement)['state'],'waiting')
        # A different worker/instance/token cannot release the active claim.
        self.control.command('headless',{'operation':'release','instance':'original','token':grant['token']})
        self.assertEqual(self.claim(worker='headless',job=replacement)['state'],'waiting')
        self.control.command('msi',{'operation':'release','instance':'original','token':grant['token']})
        self.assertEqual(self.claim(worker='headless',job=replacement)['state'],'granted')

    def test_lost_grant_response_retains_receipt_and_can_be_recovered(self):
        root=self.base/'msi';ensure_identity(root,'msi')
        server=ReportingServer(self.base/'main',self.base/'main-ensemble',{'bind':'127.0.0.1','port':0,'tokens':{'msi':'t'*40}},command_plan=self.path).start()
        self.addCleanup(server.close)
        reporter=Reporter(root,self.base/'ensemble','msi',{'worker_id':'msi','url':'http://127.0.0.1:'+str(server.server.server_port),'token':'t'*40})
        client=AssignmentClient(root,reporter,self.plan);real=reporter.request
        def lose_response(path,value=None,headers=None):
            result=real(path,value,headers)
            if path=='/v1/assignments' and value.get('operation')=='claim':raise OSError('lost acknowledgment')
            return result
        with patch.object(reporter,'request',side_effect=lose_response):self.assertFalse(client.claim(self.job))
        self.assertTrue(receipt_path(root).exists());self.assertEqual(self.claim(instance='other')['state'],'waiting')
        self.assertTrue(client.claim(self.job));self.assertTrue(receipt_path(root).exists())
        self.assertNotIn('token',client.status)
        client.release();self.assertFalse(receipt_path(root).exists())

    def test_supervised_skip_inventory_ignores_unknown_legacy_claims(self):
        from collection.ensembleProgress import shared_progress,FORMAT
        from storage.pandasStore import atomic_json
        path=self.base/'ensemble/provenance.json'
        row={'namespace':'sofascore','competition':'Premier League','season':'2024-2025','match_id':'1',
             'status':'complete','capabilities':['provider:sofascore'],'origins':['old'],'holders':['old']}
        atomic_json(path,{'format':FORMAT,'records':{'one':row}})
        self.assertIn('one',shared_progress(path,local_worker='msi'))
        self.assertEqual(shared_progress(path,local_worker='msi',trusted=True),{})
        atomic_json(path.parent/'reporting/workers/headless.json',{'format':FORMAT,'worker_id':'headless','records':{'one':row}})
        self.assertIn('one',shared_progress(path,local_worker='msi',trusted=True))

    def test_main_unavailable_does_not_authorize_new_work(self):
        class Offline:
            url=None
            identity={'instance_id':'original'}
            def request(self,*args):raise OSError('offline')
            def pull(self):raise OSError('offline')
        client=AssignmentClient(self.base/'msi',Offline(),self.plan)
        self.assertFalse(client.claim(self.job));self.assertEqual(client.status['state'],'waiting')


if __name__=='__main__':unittest.main()

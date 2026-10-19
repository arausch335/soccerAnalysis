from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
import config
from config.workerPlan import load_plan, requested_resources
import main


class WorkerPlanTests(unittest.TestCase):
    def setUp(self):self.document=yaml.safe_load((config.PROJECT_ROOT/'config/workers.yaml').read_text())

    def load(self,document):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'workers.yaml';path.write_text(yaml.safe_dump(document))
            return load_plan(path)

    def test_mac_preserved_msi_unassigned(self):
        plan=self.load(self.document)
        self.assertEqual(len(plan.for_worker('main')),7)
        self.assertEqual(plan.for_worker('msi'),[])
        self.assertTrue(all(j.as_dict()['season']=='2023-2024' for j in plan.for_worker('main')))
        self.assertNotIn('fotmob_stats',requested_resources(['combined'],'fast'))
        self.assertIn('whoscored_events',requested_resources(['combined'],'fast'))

    def test_overlap_rejected_but_disjoint_provider_allowed(self):
        item=deepcopy(self.document['workers']['main']['assignments'][0])
        self.document['workers']['msi']['assignments']=[item]
        with self.assertRaisesRegex(config.ConfigError,'Overlapping'):self.load(self.document)
        item['collection_types']=['fotmob_players']
        self.assertEqual(len(self.load(self.document).for_worker('msi')),7)

    def test_unknown_worker_and_unsafe_fields_rejected(self):
        with self.assertRaisesRegex(config.ConfigError,'not in'):self.load(self.document).for_worker('unknown')
        for section,field,value in [('git','branch','main; rm'),('git','poll_seconds',0),('git','enabled','yes'),('dashboard','port',80)]:
            document=deepcopy(self.document);document[section][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.load(document)

    def test_entrypoint_keeps_explicit_legacy_commands(self):
        with patch('main.main',return_value=8) as old,patch('collection.worker.entrypoint',return_value=9) as worker:
            self.assertEqual(main.entrypoint([]),9)
            self.assertEqual(main.entrypoint(['--worker-id','msi','--once']),9)
            self.assertEqual(main.entrypoint(['--competition','La Liga','--season','2023/24']),8)
            self.assertEqual(main.entrypoint(['--inventory','--worker-id','main']),8)
            self.assertEqual(old.call_count,2);self.assertEqual(worker.call_count,2)


from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from concordance.publication import read_table,read_registry,validate
from concordance.registry import Registry
from concordance.teamBridge import recover_fixture_aliases
from dataset_pipeline.identity import annotate
from dataset_pipeline.review import create_review
from dataset_pipeline.handoff import create,verify
from tests import test_parquet_concordances as fixtures


class FixtureTeamBridgeTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ParquetTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.registry=self.case.registry
        for row in self.case.rows:
            row['baseline_provider']='sofascore';row['whoscored_fixture_check']='validated'
        self.home=self.case.rows[0]['home_entity_id'];self.away=self.case.rows[0]['away_entity_id']

    def unknown_home_alias(self):
        self.case.rows[0]['whoscored_home_team_id']='2832'
        # Evidence at home only: the next fixture has no provider-side team ID.
        row=self.case.rows[1]
        for key in ('entity_id','team_id','team','safe_for_cross_provider_joins','team_identity_status'):
            left='home_'+key;right='away_'+key
            if left in row or right in row:row[left],row[right]=row.get(right,''),row.get(left,'')
        for key in ('alex_{}_team_id','reep_{}_team_id'):
            left=key.format('home');right=key.format('away');row[left],row[right]=row.get(right,''),row.get(left,'')
        for provider in ('sofascore','fotmob','whoscored'):
            left=provider+'_home_team_id';right=provider+'_away_team_id'
            row[left],row[right]=row[right],row[left]
        row['whoscored_away_team_id']=''

    def test_home_evidence_recovers_alias_for_both_home_and_away_rows(self):
        self.unknown_home_alias();before=deepcopy(self.registry.value['redirects'])
        audit=recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),self.home)
        recovered=[r for r in audit['recovered'] if r['provider_team_id']=='2832']
        self.assertEqual(len(recovered),1);self.assertEqual(recovered[0]['home_evidence'],1)
        self.assertEqual(self.registry.value['redirects'],before)

    def test_away_only_evidence_also_supplies_a_global_bridge(self):
        self.case.rows[0]['whoscored_away_team_id']='2832'
        audit=recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),self.away)
        recovered=next(r for r in audit['recovered'] if r['provider_team_id']=='2832')
        self.assertEqual(recovered['away_evidence'],1);self.assertEqual(recovered['home_evidence'],0)

    def test_recovery_is_provider_scoped_for_all_three_namespaces(self):
        for provider,native in [('sofascore','90001'),('fotmob','90002'),('whoscored','90003')]:
            self.case.rows[0][provider+'_home_team_id']=native
        recover_fixture_aliases(self.case.rows,self.registry)
        for provider,native in [('sofascore','90001'),('fotmob','90002'),('whoscored','90003')]:
            self.assertEqual(self.registry.lookup('team',provider,native),self.home)

    def test_conflicting_sides_do_not_choose_a_mapping_by_order_or_frequency(self):
        self.unknown_home_alias()
        self.case.rows[2]['whoscored_away_team_id']='2832'
        audit=recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),'')
        self.assertTrue(any(r['provider_team_id']=='2832' and r['reason']=='conflicting_fixture_sides' for r in audit['conflicts']))

    def test_unvalidated_or_unmatched_fixtures_do_not_create_aliases(self):
        self.unknown_home_alias();self.case.rows[0]['whoscored_fixture_check']='bridge_only'
        recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),'')
        self.case.rows[0]['whoscored_fixture_check']='validated'
        self.case.rows[0]['whoscored_match_id']='99999'
        recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),'')

    def test_existing_and_withheld_mappings_remain_authoritative(self):
        self.unknown_home_alias()
        self.registry.value['aliases']['team|whoscored|2832']=self.case.rows[0]['alex_away_team_id']
        audit=recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),self.away)
        self.assertTrue(any(r['reason']=='existing_mapping_disagrees' for r in audit['conflicts']))
        self.registry.withhold('team','whoscored','2832','reviewed_rejection')
        recover_fixture_aliases(self.case.rows,self.registry)
        self.assertEqual(self.registry.lookup('team','whoscored','2832'),'')

    def test_existing_alias_is_exported_even_when_absent_from_primary_and_fixture_columns(self):
        alias=self.case.rows[0]['alex_home_team_id']
        self.registry.value['aliases']['team|whoscored|2832']=alias
        self.case.write()
        metadata=read_table(self.case.dataset/'concordance','metadata/team_ids').fillna('')
        row=metadata[metadata.entity_id.eq(self.home)].iloc[0]
        self.assertIn('2832',json.loads(row.provider_aliases)['whoscored_id'])
        self.assertEqual(read_table(self.case.dataset/'concordance','team_ids').set_index('entity_id').loc[self.home,'whoscored_id'],'111')

    def test_publication_recovers_seventeen_rows_and_dataset_only_r_bridge(self):
        self.unknown_home_alias();self.case.write()
        validate(self.case.dataset/'concordance')
        registry=Registry(self.case.dataset/'unused',value=read_registry(self.case.dataset/'concordance'))
        self.assertEqual(registry.lookup('team','whoscored','2832'),self.home)
        target=self.case.dataset/'raw_data/premier-league/2024-2025';target.mkdir(parents=True)
        rows=[{'match_id':'100','provider':'whoscored','provider_match_id':'300','provider_team_id':'2832','side':'home'}]
        rows.extend({'match_id':'101','provider':'whoscored','provider_match_id':'301','provider_team_id':'2832','side':''} for _ in range(16))
        path=target/'team_match_stats.csv';pd.DataFrame(rows).to_csv(path,index=False)
        audit=annotate(self.case.dataset)
        self.assertEqual(audit['summary']['status_counts'],{'resolved':17})
        frame=pd.read_csv(path,dtype=str)
        self.assertEqual(set(frame.team_entity_id),{self.home})
        create_review(self.case.dataset);create(self.case.dataset);verify(self.case.dataset)
        from collection.datasetPackaging import package_saved
        before=path.read_bytes()
        with patch('dataset_pipeline.build.package_collection',side_effect=AssertionError('season export during reference repair')):
            self.assertEqual(package_saved(self.case.root,self.case.dataset,references_only=True,report=lambda *a,**k:None),0)
        self.assertEqual(path.read_bytes(),before);verify(self.case.dataset)
        if not shutil.which('Rscript'):return
        destination=self.case.base/'R dataset only'/'dataset'
        shutil.copytree(self.case.dataset,destination)
        program="""root=commandArgs(TRUE)[1];source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root))
b=subset(ds$concordance$team_bridge,provider=='whoscored')
stopifnot(!anyDuplicated(b$provider_team_id),sum(b$provider_team_id=='2832')==1)
x=read_dataset_table(ds,'team_match_stats')
resolved=b$entity_team_id[match(x$provider_team_id,b$provider_team_id)]
stopifnot(nrow(x)==17,!anyNA(resolved),all(resolved==x$team_entity_id))
"""
        result=subprocess.run(['Rscript','--vanilla','-e',program,str(destination)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

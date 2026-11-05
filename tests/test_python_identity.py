"""General provider aliases, fixture evidence and explicit unresolved ID reporting."""
from pathlib import Path
import csv
import json
import shutil
import unittest
from unittest.mock import patch

import pandas as pd

from dataset_identity import Resolver,annotate
from dataset_review import create_review,load_review
from collection.datasetPackaging import package_saved
from collection.storageServer import Archive
from collection.workerIdentity import ensure_identity
from storage.pandasStore import atomic_json
from tests import test_parquet_concordances as fixtures
from tests.test_explicit_dataset import ExplicitDatasetTests


class PythonIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ParquetTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        for row in self.case.rows:row['baseline_provider']='sofascore'
        self.case.registry.value['aliases']['team|sofascore|6070']=self.case.registry.value['aliases']['team|sofascore|1']
        native={'sofascore_id':'7','fotmob_id':'70','whoscored_id':'700'}
        self.player={**native,**self.case.registry.assign('player',native),'player_name':'Known Player'}
        self.case.write(players=[self.player],chunk_rows=2)
        self.root=self.case.dataset
        self.season=self.root/'raw_data/premier-league/2024-2025';self.season.mkdir(parents=True)

    def write(self,rows,name='player_match_stats'):
        if name=='player_match_stats':rows=[{'player_id':'7',**row} for row in rows]
        path=self.season/(name+'.csv');pd.DataFrame(rows).to_csv(path,index=False);return path

    def records(self,path):
        with path.open(newline='',encoding='utf-8') as stream:return list(csv.DictReader(stream))

    def test_every_native_alias_is_resolved_without_changing_original_ids(self):
        path=self.write([{'match_id':'100','team_id':'6070','opponent_id':'2'},
                         {'match_id':'101','team_id':'2','opponent_id':'6070'}])
        audit=annotate(self.root);rows=self.records(path)
        self.assertEqual(rows[0]['team_id'],'6070');self.assertEqual(rows[1]['opponent_id'],'6070')
        home=Resolver(self.root).resolve('team','sofascore','1')
        self.assertEqual(rows[0]['team_entity_id'],home);self.assertEqual(rows[1]['opponent_entity_id'],home)
        self.assertEqual(audit['summary']['missing_ids'],{})
        self.assertEqual(audit['summary']['recovery_methods']['registry_alias'],2)
        before=(path.read_bytes(),path.stat().st_mtime_ns,(self.root/'id-resolution.json').read_bytes())
        annotate(self.root)
        self.assertEqual(before,(path.read_bytes(),path.stat().st_mtime_ns,(self.root/'id-resolution.json').read_bytes()))

    def test_aliases_and_redirects_apply_to_all_three_provider_namespaces(self):
        aliases={'sofascore':'90001','fotmob':'90002','whoscored':'90003'}
        native={'sofascore_id':'80001','fotmob_id':'80002','whoscored_id':'80003'}
        player={**native,**self.case.registry.assign('player',native),'player_name':'Known Player'}
        for provider,alias in aliases.items():
            self.case.registry.value['aliases']['team|'+provider+'|'+alias]=self.case.registry.value['aliases']['team|'+provider+'|'+{'sofascore':'1','fotmob':'11','whoscored':'111'}[provider]]
        self.case.write(players=[player])
        source=self.case.base/'source'
        for provider,match,opponent in [('sofascore','100','2'),('fotmob','200','22'),('whoscored','300','222')]:
            with self.subTest(provider=provider):
                atomic_json(source/'premier-league/2024-2025/config.json',{'id_namespace':provider})
                path=self.write([{'match_id':match,'team_id':aliases[provider],'opponent_id':opponent,'player_id':native[provider+'_id']}])
                audit=annotate(self.root,source);row=self.records(path)[0]
                self.assertEqual(row['identity_resolution_status'],'resolved')
                self.assertEqual(row['team_id'],aliases[provider]);self.assertTrue(row['player_entity_id'])
                self.assertEqual(audit['summary']['recovery_methods']['registry_alias'],1)

    def test_fixture_scoped_names_sides_and_opponent_recovery_work_for_any_provider(self):
        path=self.write([{'match_id':'100','team_id':'unknown','team':'Home','opponent_id':'2'},
                         {'match_id':'100','team_id':'1','opponent_id':'unknown'},
                         {'match_id':'100','team_id':'unknown','opponent_id':'unknown'}])
        team=self.write([{'match_id':'100','provider':'fotmob','provider_match_id':'200','provider_team_id':'unknown','side':'away'}],'team_match_stats')
        audit=annotate(self.root);rows=self.records(path);t=self.records(team)[0]
        self.assertTrue(rows[0]['team_entity_id']);self.assertTrue(rows[1]['opponent_entity_id'])
        self.assertEqual(rows[2]['identity_resolution_status'],'unresolved')
        self.assertEqual(t['team_entity_id'],rows[0]['opponent_entity_id'])
        self.assertEqual(t['opponent_entity_id'],rows[0]['team_entity_id'])
        self.assertEqual(audit['summary']['status_counts'],{'resolved':3,'unresolved':1})

    def test_conflicting_namespaces_and_wrong_fixture_members_are_not_guessed(self):
        path=self.write([{'match_id':'100','provider':'fotmob','provider_match_id':'201','provider_team_id':'11','side':'home'}],'team_match_stats')
        audit=annotate(self.root);row=self.records(path)[0]
        self.assertEqual(row['match_entity_id'],'');self.assertIn('conflicting_match_ids',row['identity_resolution_issues'])
        self.assertEqual(audit['summary']['status_counts']['conflict'],1)
        # A known canonical team outside the fixture must not be silently repaired.
        from concordance.publication import publish,read_table,folder
        from concordance.registry import Registry
        native={'sofascore_id':'999'};extra={**native,**self.case.registry.assign('team',native),'team_name':'Other'}
        self.case.write(teams=[extra]);resolver=Resolver(self.root)
        result,_=resolver.row({'match_id':'100','team_id':'999','opponent_id':'2'},'player_match_stats','sofascore')
        self.assertEqual(result['team_entity_id'],'');self.assertIn('team_entity_id_outside_fixture',result['identity_resolution_issues'])

    def test_fixture_native_team_ids_must_agree_with_the_declared_sides(self):
        result,_=Resolver(self.root).row({'match_id':'100','home_team_id':'2','away_team_id':'1'},'fixtures','sofascore')
        self.assertEqual(result['identity_resolution_status'],'conflict')
        self.assertEqual(result['home_team_entity_id'],'');self.assertEqual(result['away_team_entity_id'],'')
        self.assertIn('home_team_entity_id_wrong_side',result['identity_resolution_issues'])

    def test_provider_side_disagreement_is_retained_for_review(self):
        resolver=Resolver(self.root)
        result,_=resolver.row({'match_id':'100','team_id':'1','side':'away'},'team_match_stats','sofascore')
        self.assertEqual(result['team_entity_id'],'');self.assertIn('team_side_disagrees',result['identity_resolution_issues'])

    def test_duplicate_team_pair_is_not_filled_as_a_valid_matchup(self):
        result,_=Resolver(self.root).row({'match_id':'100','team_id':'1','opponent_id':'1'},'player_match_stats','sofascore')
        self.assertEqual(result['identity_resolution_status'],'conflict');self.assertEqual(result['team_entity_id'],'')

    def test_missing_schedule_is_deferred_and_not_acknowledged_then_runs_when_ready(self):
        helper=ExplicitDatasetTests();helper.setUp();self.addCleanup(helper.doCleanups)
        source=helper.saved_match();archive=Archive(helper.root);archive.schedule_build('premier-league/2024-2025')
        stub=helper.root/'ligue-1/2026-2027';atomic_json(stub/'job.json',{'competition':'Ligue 1','season':'2026/27','providers':['sofascore']})
        atomic_json(stub/'config.json',{'competition':'Ligue 1','season':'2026/27'})
        archive.schedule_build('ligue-1/2026-2027')
        self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),0)
        review=load_review(helper.output);self.assertEqual(review['deferred_seasons'][0]['missing_inputs'],['schedule.json'])
        self.assertEqual(next(r['state'] for r in archive.diagnostics()['builds'] if r['season']=='ligue-1/2026-2027'),'waiting_inputs')
        self.assertNotIn('premier-league/2024-2025',dict(archive.build_jobs()))
        self.assertTrue(review['identity_resolution']);self.assertTrue((helper.output/'id-resolution.json').is_file())
        # A repeated unchanged package leaves the deferred job outstanding.
        first=archive.publication()
        self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),0)
        self.assertEqual(first,archive.publication())

    def test_missing_config_and_schedule_stub_does_not_block_other_seasons(self):
        helper=ExplicitDatasetTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.saved_match();stub=helper.root/'ligue-1/2026-2027'
        atomic_json(stub/'job.json',{'competition':'Ligue 1','season':'2026/27'})
        self.assertEqual(package_saved(helper.root,helper.output,report=lambda *a,**k:None),0)
        review=load_review(helper.output)
        self.assertEqual(review['deferred_seasons'][0]['missing_inputs'],['config.json','schedule.json'])

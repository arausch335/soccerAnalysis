"""Main authority, stable fixture keys, and nonblocking disputed player links."""
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from concordance.registry import Registry
from concordance.sync import load,install,snapshot,local_path,version
from fixtures.sharedIds import assign_ids
from providers import reep
from storage.pandasStore import atomic_json


class SharedRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'worker'
        self.folder=self.base/'dataset/fixture_concordance'

    def install_bridges(self,rows):
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})

    def test_main_alex_key_wins_over_existing_orphan_alias_for_all_entity_kinds(self):
        for kind in ('match','team','player'):
            with self.subTest(kind=kind):
                main=Registry(self.base/(kind+'-main.json'))
                preferred=main.assign(kind,{'sofascore_id':'1'})
                old=main.assign(kind,{'fotmob_id':'2'})
                main.value['aliases'].pop(kind+'|fotmob|2')
                worker=Registry(self.base/(kind+'-worker.json'))
                worker.assign(kind,{'fotmob_id':'2'})
                worker.assign(kind,{'sofascore_id':'1','fotmob_id':'2'})
                main.merge(worker.value)
                self.assertEqual(main.lookup(kind,'fotmob','2'),preferred['entity_id'])
                self.assertEqual(main.canonical(old['entity_id']),preferred['entity_id'])
                self.assertFalse(any(i['reason']=='registry_alias_disagreement' for i in main.issues))
                main.save();again=Registry(main.path);again.merge(worker.value)
                self.assertEqual(again.canonical(old['entity_id']),preferred['entity_id'])
                self.assertGreater(again.alex_aliases_resolved(),0)

    def test_alex_fallback_choice_is_deterministic_and_cannot_merge_distinct_baselines(self):
        registry=Registry(self.base/'ids.json')
        a=registry.assign('player',{'fotmob_id':'1'})['alex_id']
        b=registry.assign('player',{'whoscored_id':'2'})['alex_id']
        self.assertEqual(registry.select_alex_id('player',[b,a]),min(a,b))
        self.assertEqual(registry.follow(a),registry.follow(b))
        one=registry.assign('team',{'sofascore_id':'1'})['alex_id']
        two=registry.assign('team',{'sofascore_id':'2'})['alex_id']
        before=json.dumps(registry.value,sort_keys=True)
        with self.assertRaisesRegex(ValueError,'distinct baseline'):
            registry.select_alex_id('team',[one,two])
        self.assertEqual(before,json.dumps(registry.value,sort_keys=True))

    def test_alex_choice_cannot_hide_conflicting_published_ids(self):
        registry=Registry(self.base/'ids.json')
        a=registry.assign('player',{'sofascore_id':'1'},'rp-one')['alex_id']
        b=registry.assign('player',{'fotmob_id':'2'},'rp-two')['alex_id']
        with self.assertRaisesRegex(ValueError,'contradictory published'):
            registry.select_alex_id('player',[a,b])

    def test_fixture_match_and_team_references_reuse_selected_main_ids(self):
        main=Registry(local_path(self.root));worker=Registry(self.folder/'alex_registry.json')
        expected={};old={}
        for kind,sofa,fot in [('match','1','2'),('team','10','20'),('team','11','21')]:
            expected[kind,sofa]=main.assign(kind,{'sofascore_id':sofa})['entity_id']
            old[kind,sofa]=main.assign(kind,{'fotmob_id':fot})['entity_id']
            main.value['aliases'].pop(f'{kind}|fotmob|{fot}')
            worker.assign(kind,{'fotmob_id':fot})
            worker.assign(kind,{'sofascore_id':sofa,'fotmob_id':fot})
        main.save();worker.save()
        fixtures=[{'sofascore_match_id':'1','fotmob_match_id':'2',
            'sofascore_home_team_id':'10','fotmob_home_team_id':'20',
            'sofascore_away_team_id':'11','fotmob_away_team_id':'21'}]
        matches=[{'sofascore_id':'1','fotmob_id':'2'}]
        teams=[{'sofascore_id':'10','fotmob_id':'20'},{'sofascore_id':'11','fotmob_id':'21'}]
        detail=assign_ids(self.root,fixtures,matches,teams,folder=self.folder)
        self.assertEqual(fixtures[0]['entity_id'],expected['match','1'])
        self.assertEqual(fixtures[0]['entity_id'],matches[0]['entity_id'])
        self.assertEqual(fixtures[0]['home_entity_id'],teams[0]['entity_id'])
        self.assertEqual(fixtures[0]['away_entity_id'],teams[1]['entity_id'])
        restored=Registry(local_path(self.root))
        for key,value in old.items():self.assertEqual(restored.canonical(value),expected[key])
        self.assertEqual(detail['alex_id_aliases_resolved'],3)
        self.assertEqual(detail['registry_conflicts'],0)

    def test_main_snapshot_merges_existing_worker_and_redirects_old_project_id(self):
        main=self.base/'main';authority=Registry(local_path(main))
        expected=authority.assign('match',{'sofascore_id':'1','fotmob_id':'2'});authority.save()
        local=Registry(local_path(self.root));old=local.assign('match',{'fotmob_id':'2'})
        local.assign('match',{'sofascore_id':'1','fotmob_id':'2'});local.save()
        install(self.root,snapshot(main));merged=load(self.root)
        self.assertEqual(merged.lookup('match','fotmob','2'),expected['entity_id'])
        self.assertEqual(merged.canonical(old['entity_id']),expected['entity_id'])
        merged.save();self.assertEqual(load(self.root).lookup('match','sofascore','1'),expected['entity_id'])

    def test_merging_registry_preserves_older_project_redirects(self):
        source=Registry(self.base/'source.json')
        first=source.assign('match',{'fotmob_id':'2'})
        source.assign('match',{'sofascore_id':'1'})
        joined=source.assign('match',{'sofascore_id':'1','fotmob_id':'2'})
        target=Registry(self.base/'target.json');target.merge(source.value)
        self.assertEqual(target.canonical(first['entity_id']),joined['entity_id'])

    def test_unrelated_existing_groups_survive_snapshot_merge(self):
        main=self.base/'main';master=Registry(local_path(main));master.assign('team',{'sofascore_id':'1'});master.save()
        local=Registry(local_path(self.root));old=local.assign('player',{'fotmob_id':'99'});local.save()
        install(self.root,snapshot(main));merged=load(self.root)
        self.assertEqual(merged.lookup('player','fotmob','99'),old['entity_id'])

    def test_stale_published_id_does_not_abort_fixture_export(self):
        registry=Registry(local_path(self.root));registry.assign('match',{'sofascore_id':'1'},'rm-canonical');registry.save()
        fixtures=[{'sofascore_match_id':'1','fotmob_match_id':'2','reep_match_id':'rm-old',
                   'sofascore_home_team_id':'10','sofascore_away_team_id':'20'}]
        detail=assign_ids(self.root,fixtures,folder=self.folder)
        self.assertEqual(fixtures[0]['entity_id'],'rm-canonical')
        self.assertEqual(fixtures[0]['reep_match_id'],'rm-canonical')
        self.assertFalse(fixtures[0]['safe_for_cross_provider_joins'])
        self.assertGreater(detail['registry_conflicts'],0)
        self.assertIn('rm-old',(self.folder/'registry_conflicts.csv').read_text())

    def test_compiled_registry_is_merged_even_when_local_registry_exists(self):
        local=Registry(local_path(self.root));local.assign('team',{'sofascore_id':'9'});local.save()
        incoming=Registry(self.folder/'alex_registry.json')
        old=incoming.assign('match',{'fotmob_id':'2'});incoming.assign('match',{'sofascore_id':'1','fotmob_id':'2'});incoming.save()
        rows=[{'sofascore_id':'1','fotmob_id':'2'}]
        assign_ids(self.root,[],matches=rows,folder=self.folder)
        self.assertEqual(rows[0]['entity_id'],old['entity_id'])
        self.assertTrue(Registry(local_path(self.root)).lookup('team','sofascore','9'))

    def test_player_spelling_aliases_keep_one_identity_and_fotmob_conflict_is_withheld(self):
        self.install_bridges([
            ['player','sofascore','997082','rp-oleg'],['player','fotmob','1192419','rp-oleg'],['player','whoscored','427997','rp-oleg'],
            ['player','sofascore','1010028','rp-yakine'],['player','whoscored','438191','rp-yakine']])
        rows=[{'player_name':name,'sofascore_id':'997082','fotmob_id':'1192419','whoscored_id':'427997'} for name in ('Oleg Ocheretko','Oleh Ocheretko')]
        rows += [{'player_name':'Yakine Said','sofascore_id':'1010028','fotmob_id':fot,'whoscored_id':'438191'} for fot in ('1354467','1354468')]
        assign_ids(self.root,[],players=rows,folder=self.folder)
        self.assertEqual({r['entity_id'] for r in rows[:2]},{'rp-oleg'})
        self.assertEqual(json.loads(rows[0]['name_aliases']),['Oleg Ocheretko','Oleh Ocheretko'])
        self.assertTrue(all(r['safe_for_cross_provider_joins'] for r in rows[:2]))
        self.assertEqual({r['entity_id'] for r in rows[2:]},{'rp-yakine'})
        self.assertTrue(all(r['fotmob_id']=='' and r['whoscored_id']=='438191' for r in rows[2:]))
        self.assertFalse(any(r['safe_for_cross_provider_joins'] for r in rows[2:]))
        review=pd.read_csv(self.folder/'registry_conflicts.csv',dtype=str)
        self.assertTrue({'1354467','1354468'}<=set(review.native_id))
        # A later build of just one stint must not revive the disputed mapping.
        again=[{'player_name':'Yakine Said','sofascore_id':'1010028','fotmob_id':'1354467','whoscored_id':'438191'}]
        assign_ids(self.root,[],players=again,folder=self.folder)
        self.assertEqual(again[0]['fotmob_id'],'')
        assign_ids(self.root,[],players=again,folder=self.folder)
        self.assertEqual(again[0]['whoscored_id'],'438191')

    def test_player_id_table_build_continues_with_spelling_aliases_and_conflicts(self):
        from unittest.mock import patch
        import dataset_builder
        self.install_bridges([
            ['player','sofascore','1','rp-one'],['player','fotmob','10','rp-one'],['player','whoscored','100','rp-one'],
            ['player','sofascore','2','rp-two'],['player','whoscored','200','rp-two']])
        def source(_path,players,teams,matches,_root):
            for season,name,sofa,fot,who in [('2021-2022','Oleg','1','10','100'),('2022-2023','Oleh','1','10','100'),
                                          ('2021-2022','Yakine','2','20','200'),('2022-2023','Yakine','2','21','200')]:
                row=dataset_builder._id_row(players,(season,sofa),{'competition':'La Liga','season':season,'team_name':'Club','player_name':name})
                row['_ids']={'sofascore_id':[sofa],'fotmob_id':[fot],'whoscored_id':[who]}
        with patch('dataset_builder.discover_collections',return_value=[self.root/'la-liga/2021-2022']),patch('dataset_builder._source_id_rows',side_effect=source):
            written=dataset_builder.build_id_tables(self.root,self.base/'dataset',build_team_ids=False,build_match_ids=False)
        rows=pd.read_csv(written['player IDs'],dtype=str,keep_default_na=False)
        self.assertEqual(len(rows),4);self.assertEqual(set(rows.entity_id),{'rp-one','rp-two'})
        self.assertTrue(rows.loc[rows.entity_id.eq('rp-two'),'fotmob_id'].eq('').all())
        self.assertTrue((self.base/'dataset/concordance/registry_conflicts.csv').is_file())

    def test_multiple_candidate_ids_within_one_stint_are_not_chosen_by_frequency(self):
        import dataset_builder
        rows={'one':{'competition':'La Liga','season':'2021-2022','team_name':'Club','player_name':'Yakine',
            '_ids':{'sofascore_id':['1'],'fotmob_id':['20','20','21'],'whoscored_id':['100']}}}
        frame=dataset_builder._finalize_id_rows(rows,dataset_builder.PLAYER_ID_COLUMNS,['player_name'])
        records=frame.to_dict('records');assign_ids(self.root,[],players=records,folder=self.folder)
        self.assertEqual(records[0]['fotmob_id'],'');self.assertFalse(records[0]['safe_for_cross_provider_joins'])
        self.assertIn('21',(self.folder/'registry_conflicts.csv').read_text())

    def test_bad_peer_does_not_erase_another_baselines_authoritative_mapping(self):
        registry=Registry(local_path(self.root))
        first=registry.assign('match',{'sofascore_id':'1','fotmob_id':'2'})
        observed=registry.reconcile('match',{'sofascore_id':'3','fotmob_id':'2'})
        self.assertEqual(observed['rejected_ids'],{'fotmob':'2'})
        self.assertEqual(registry.lookup('match','fotmob','2'),first['entity_id'])

    def test_snapshot_checksum_failure_preserves_previous_cache(self):
        main=self.base/'main';registry=Registry(local_path(main));registry.assign('team',{'sofascore_id':'1'});registry.save()
        value=snapshot(main);install(self.root,value)
        corrupt=json.loads(json.dumps(value));corrupt['registry']['aliases'].clear()
        with self.assertRaises(ValueError):install(self.root,corrupt)
        self.assertEqual(load(self.root).lookup('team','sofascore','1'),registry.lookup('team','sofascore','1'))

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from concordance.registry import Registry
from concordance.build import rebuild,validate_bundle,TABLES,invalidate_bundle
from collection.storageTransfer import safe_path
from collection.workerIdentity import ensure_identity
from storage.pandasStore import atomic_json
from tools.audit_integration import audit,now,EXPECTED


class RegistryTests(unittest.TestCase):
    def test_stable_namespaces_and_later_reep_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ids.json';registry=Registry(path)
            first=registry.assign('player',{'fotmob_id':'12'})
            self.assertTrue(first['entity_id'].startswith('ap'));self.assertEqual(first['reep_id'],'')
            registry.save();registry=Registry(path)
            enriched=registry.assign('player',{'fotmob_id':'12','sofascore_id':'34'})
            self.assertEqual(first['entity_id'],enriched['entity_id'])
            linked=registry.assign('player',{'sofascore_id':'34'},'rp-published')
            self.assertEqual(linked['entity_id'],'rp-published')
            self.assertIn({'from_id':first['entity_id'],'to_id':'rp-published'},registry.redirects())
            self.assertNotEqual(registry.assign('match',{'fotmob_id':'12'})['entity_id'],first['entity_id'])

    def test_conflict_cannot_link_an_unapproved_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=Registry(Path(tmp)/'ids.json')
            a=registry.assign('team',{'sofascore_id':'1','fotmob_id':'2'},conflict=True)
            b=registry.assign('team',{'fotmob_id':'2'})
            self.assertNotEqual(a['entity_id'],b['entity_id'])
            with self.assertRaises(ValueError):registry.assign('match',{})

    def test_new_baseline_cannot_reuse_another_baselines_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=Registry(Path(tmp)/'ids.json')
            registry.assign('team',{'fotmob_id':'2'})
            registry.assign('team',{'sofascore_id':'1','fotmob_id':'2'},'rt-known')
            before=json.dumps(registry.value,sort_keys=True)
            with self.assertRaisesRegex(ValueError,'baseline entities'):
                registry.assign('team',{'sofascore_id':'3','fotmob_id':'2'})
            self.assertEqual(before,json.dumps(registry.value,sort_keys=True))

    def test_existing_approved_multi_profile_identity_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=Registry(Path(tmp)/'ids.json')
            registry.assign('player',{'sofascore_id':'1','fotmob_id':'2'},'rp-known')
            registry.assign('player',{'sofascore_id':'3','fotmob_id':'2'},'rp-known')
            self.assertEqual(registry.assign('player',{'sofascore_id':'1'})['entity_id'],'rp-known')
            registry.save();registry=Registry(registry.path)
            self.assertEqual(registry.assign('player',{'fotmob_id':'2'})['entity_id'],'rp-known')

    def test_merging_provider_only_alias_preserves_existing_baseline_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=Registry(Path(tmp)/'ids.json')
            baseline=registry.assign('match',{'sofascore_id':'1'})
            peer=registry.assign('match',{'fotmob_id':'2'})
            joined=registry.assign('match',{'sofascore_id':'1','fotmob_id':'2'})
            self.assertEqual(joined['entity_id'],baseline['entity_id'])
            self.assertEqual(registry.lookup('match','fotmob','2'),baseline['entity_id'])
            self.assertIn({'from_id':peer['entity_id'],'to_id':baseline['entity_id']},registry.redirects())


class IntegrationTests(unittest.TestCase):
    def test_queue_empty_does_not_imply_all_data_integrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';ensure_identity(root,'msi')
            dbpath=root/'_system/storage/hub/archive.sqlite';dbpath.parent.mkdir(parents=True)
            with sqlite3.connect(dbpath) as db:db.execute('CREATE TABLE uploads(state TEXT)')
            expected=Path(tmp)/'expected.json'
            atomic_json(expected,{'format':EXPECTED,'generated_at':now().isoformat(),'workers':[
                {'worker':'headless','last_seen':now().isoformat(),'inventory_updated_at':now().isoformat(),
                 'runtime_updated_at':now().isoformat(),'records':{'match':{
                    'capabilities':['provider:sofascore'],'resources':{'player_stats':'complete'}}}}]})
            missing=audit(root,Path(tmp)/'ensemble',expected,scan=lambda r:{})
            self.assertEqual(missing['state'],'not_ready');self.assertEqual(len(missing['missing']),1)
            ready=audit(root,Path(tmp)/'ensemble',expected,scan=lambda r:{'match':{
                'capabilities':['provider:sofascore'],'resources':{'player_stats':'complete'}}})
            self.assertEqual(ready['state'],'ready')
            from datetime import timedelta
            value=json.loads(expected.read_text());value['workers'][0]['inventory_updated_at']=(now()-timedelta(days=2)).isoformat()
            atomic_json(expected,value)
            stale=audit(root,Path(tmp)/'ensemble',expected,scan=lambda r:{'match':{
                'capabilities':['provider:sofascore'],'resources':{'player_stats':'complete'}}})
            self.assertEqual(stale['state'],'not_ready');self.assertIn('Stale or missing worker inventory: headless',stale['problems'])
            with sqlite3.connect(dbpath) as db:db.execute("INSERT INTO uploads VALUES('received')")
            blocked=audit(root,Path(tmp)/'ensemble',expected,scan=lambda r:{'match':{
                'capabilities':['provider:sofascore'],'resources':{'player_stats':'complete'}}})
            self.assertEqual(blocked['state'],'not_ready')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.data=self.base/'dataset'
        self.data.mkdir();self.root.mkdir()
        fixtures=[dict(match_id=str(mid),sofascore_match_id=str(mid),date='2024-08-10',
            competition='Premier League',season='2024-2025',home_team_id='1',away_team_id='2',
            home_team='Home',away_team='Away',sofascore_home_team_id='1',sofascore_away_team_id='2') for mid in (10,11)]
        pd.DataFrame(fixtures).to_csv(self.data/'fixtures.csv',index=False)
        self.raw={'match':[],'team':[],'player':[
            {'player_name':'Known','competition':'Premier League','season':'2024-2025','team_name':'Home',
             '_native':{'sofascore_id':['100'],'fotmob_id':['200']}}],
            'observed':{('sofascore','10')},'sources':['test/2024-2025']}

    def test_unified_fixtures_include_observed_stats_without_a_second_match_table(self):
        with patch('concordance.build.observations',return_value=self.raw):
            manifest=rebuild(self.root,self.data,allow_unconfirmed=True,report=lambda *a,**k:None)
        folder=self.data/'concordance';check=validate_bundle(folder)
        self.assertFalse(manifest['ready_for_R']);self.assertTrue(check['id_requirements_met'])
        self.assertEqual(check['rows']['full/fixtures.csv'],2)
        self.assertEqual(len(check['rows']),5)
        self.assertFalse((folder/'scraped/matches.csv').exists())
        fixtures=pd.read_csv(folder/'full/fixtures.csv',dtype=str).set_index('sofascore_id')
        self.assertEqual(fixtures.loc['10','has_player_stats'],'True')
        self.assertEqual(fixtures.loc['10','is_scheduled_for_scraping'],'False')
        self.assertEqual(fixtures.loc['11','has_player_stats'],'False')
        self.assertEqual(check['rows']['scraped/teams.csv'],2)
        self.assertEqual(check['rows']['scraped/players.csv'],1)
        for name in TABLES:self.assertEqual(safe_path('concordance/'+name,'dataset'),'concordance/'+name)
        players=pd.read_csv(folder/'scraped/players.csv',dtype=str,keep_default_na=False)
        self.assertTrue(players.iloc[0].entity_id.startswith('ap'))
        self.assertEqual(players.iloc[0].reep_id,'')
        with self.assertRaisesRegex(ValueError,'required'):rebuild(self.root,self.data)

    def test_checksums_reject_mixed_or_edited_bundle(self):
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        path=self.data/'concordance/scraped/players.csv';path.write_text(path.read_text()+'\n')
        with self.assertRaisesRegex(ValueError,'checksum'):validate_bundle(self.data/'concordance')

    def test_bundle_rejects_provider_key_with_wrong_canonical_even_with_valid_checksum(self):
        from concordance.build import digest
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        folder=self.data/'concordance';path=folder/'full/fixtures.csv'
        frame=pd.read_csv(path,dtype=str,keep_default_na=False);frame.loc[0,'entity_id']='am-wrong'
        frame.to_csv(path,index=False)
        manifest=json.loads((folder/'bundle.json').read_text())
        manifest['files']['full/fixtures.csv']['sha256']=digest(path)
        atomic_json(folder/'bundle.json',manifest)
        with self.assertRaisesRegex(ValueError,'Provider/canonical ID disagreement'):validate_bundle(folder)

    def test_observed_provider_only_match_survives_and_uses_real_team_references(self):
        self.raw['observed'].add(('fotmob','999'))
        self.raw['match']=[{'_native':{'fotmob_id':['999']},'_fixture':{
            'competition':'Premier League','season':'2024-2025','home_team':'Other home','away_team':'Other away',
            'fotmob_home_team_id':'91','fotmob_away_team_id':'92','home_score':'2','away_score':'1'}}]
        self.raw['team']=[{'team_name':name,'_native':{'fotmob_id':[mid]}} for name,mid in [('Other home','91'),('Other away','92')]]
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        folder=self.data/'concordance';validate_bundle(folder)
        fixtures=pd.read_csv(folder/'full/fixtures.csv',dtype=str,keep_default_na=False)
        row=fixtures[fixtures.fotmob_id.eq('999')].iloc[0]
        self.assertTrue(row.entity_id.startswith('am'));self.assertEqual(row.sofascore_id,'')
        self.assertEqual((row.has_player_stats,row.is_scheduled_for_scraping),('True','False'))
        teams=pd.read_csv(folder/'full/teams.csv',dtype=str)
        self.assertIn(row.home_entity_id,set(teams.entity_id));self.assertIn(row.away_entity_id,set(teams.entity_id))
        self.assertEqual((row.home_score,row.away_score),('2','1'))

    def test_saved_stats_without_team_metadata_are_preserved_and_flagged(self):
        self.raw['observed'].add(('fotmob','999'))
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        folder=self.data/'concordance';validate_bundle(folder)
        rows=pd.read_csv(folder/'full/fixtures.csv',dtype=str,keep_default_na=False)
        row=rows[rows.fotmob_id.eq('999')].iloc[0]
        self.assertEqual(row.has_player_stats,'True');self.assertEqual(row.fixture_metadata_complete,'False')
        self.assertEqual(row.home_entity_id,'');self.assertEqual(row.away_entity_id,'')

    def test_old_match_table_is_retired_and_old_bundle_requires_rebuild(self):
        folder=self.data/'concordance';old=folder/'scraped/matches.csv';old.parent.mkdir(parents=True)
        old.write_text('entity_id\nstale\n')
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        self.assertFalse(old.exists())
        path=folder/'bundle.json';manifest=json.loads(path.read_text());manifest['format']='soccer-concordance-bundle-v2';manifest['ready_for_R']=True
        atomic_json(path,manifest)
        self.assertTrue(invalidate_bundle(self.data,'Unified fixtures required'))
        with self.assertRaisesRegex(ValueError,'rebuild'):validate_bundle(folder)

    def test_preview_uses_the_same_registry_as_the_fixture_export(self):
        from fixtures.sharedIds import assign_ids
        fixtures=pd.read_csv(self.data/'fixtures.csv',dtype=str,keep_default_na=False).to_dict('records')
        assign_ids(self.root,fixtures)
        expected={r['sofascore_match_id']:r['entity_id'] for r in fixtures}
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        rows=pd.read_csv(self.data/'concordance/full/fixtures.csv',dtype=str).to_dict('records')
        self.assertEqual(expected,{r['sofascore_id']:r['entity_id'] for r in rows})

    def test_failed_build_preserves_previous_bundle_and_registry(self):
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        path=self.data/'concordance/bundle.json';prior=path.read_bytes()
        reg=self.data/'concordance/.preview-state/_system/identity/alex_ids.json';saved=reg.read_bytes()
        with patch('concordance.build.observations',side_effect=ValueError('bad raw archive')):
            with self.assertRaises(ValueError):rebuild(self.root,self.data,allow_unconfirmed=True)
        self.assertEqual(path.read_bytes(),prior);self.assertEqual(reg.read_bytes(),saved)

    def test_real_checkpoint_sources_recover_unpublished_fixtures_and_player_ids(self):
        from tests.test_ensemble import populate
        populate(self.root,limit=1)
        # The raw checkpoint exists, but the season CSV export was deferred.
        manifest=rebuild(self.root,self.data,allow_unconfirmed=True,report=lambda *a,**k:None)
        folder=self.data/'concordance'
        matches=pd.read_csv(folder/'full/fixtures.csv',dtype=str,keep_default_na=False)
        matches=matches[matches.has_player_stats.eq('True')]
        players=pd.read_csv(folder/'scraped/players.csv',dtype=str,keep_default_na=False)
        full=pd.read_csv(folder/'full/fixtures.csv',dtype=str,keep_default_na=False)
        self.assertIn('1',set(matches.sofascore_id))
        self.assertIn('1',set(full.sofascore_id))
        self.assertTrue(len(players)>0)
        self.assertTrue(players.entity_id.ne('').all())

    def test_new_data_invalidates_an_old_ready_snapshot(self):
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        path=self.data/'concordance/bundle.json';manifest=json.loads(path.read_text())
        manifest['ready_for_R']=True;atomic_json(path,manifest)
        self.assertTrue(invalidate_bundle(self.data,'new statistics arrived'))
        self.assertFalse(validate_bundle(self.data/'concordance')['ready_for_R'])

    def test_published_historical_team_aliases_keep_both_fixture_foreign_keys(self):
        from providers import reep
        fixtures=pd.read_csv(self.data/'fixtures.csv',dtype=str)
        fixtures.loc[1,'home_team_id']='3';fixtures.loc[1,'sofascore_home_team_id']='3'
        fixtures.to_csv(self.data/'fixtures.csv',index=False)
        rows=[['team','sofascore','1','rt-shared'],['team','sofascore','3','rt-shared'],['team','fotmob','10','rt-shared']]
        reep.install(self.root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        full=pd.read_csv(self.data/'concordance/full/fixtures.csv',dtype=str)
        self.assertEqual(set(full.home_entity_id),{'rt-shared'})
        self.assertTrue(validate_bundle(self.data/'concordance')['id_requirements_met'])

    def test_full_fixture_export_normalizes_all_retired_alex_columns(self):
        from concordance.sync import local_path
        registry=Registry(local_path(self.root));old={};chosen={}
        for kind,native,peer in [('match','10','110'),('match','11','111'),('team','1','101'),('team','2','102')]:
            chosen[kind,native]=registry.assign(kind,{'sofascore_id':native})['alex_id']
            old[kind,native]=registry.assign(kind,{'fotmob_id':peer})['alex_id']
            registry.assign(kind,{'sofascore_id':native,'fotmob_id':peer})
        registry.save()
        fixtures=pd.read_csv(self.data/'fixtures.csv',dtype=str)
        fixtures['alex_match_id']=fixtures.sofascore_match_id.map(lambda v:old['match',v])
        fixtures['alex_home_team_id']=old['team','1'];fixtures['alex_away_team_id']=old['team','2']
        fixtures.to_csv(self.data/'fixtures.csv',index=False)
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        full=pd.read_csv(self.data/'concordance/full/fixtures.csv',dtype=str)
        self.assertEqual(full.alex_match_id.tolist(),[chosen['match','10'],chosen['match','11']])
        self.assertEqual(set(full.alex_home_team_id),{chosen['team','1']})
        self.assertEqual(set(full.alex_away_team_id),{chosen['team','2']})

    def test_project_ids_survive_registry_restore_from_compiled_bundle(self):
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        folder=self.data/'concordance';first=pd.read_csv(folder/'scraped/players.csv',dtype=str).entity_id.tolist()
        (folder/'.preview-state/_system/identity/alex_ids.json').unlink()
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        self.assertEqual(first,pd.read_csv(folder/'scraped/players.csv',dtype=str).entity_id.tolist())

    def test_bundle_outputs_do_not_become_the_next_builds_source_seasons(self):
        from concordance.sources import source_signature
        before=source_signature(self.root,self.data)
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        self.assertEqual(before,source_signature(self.root,self.data))

    def test_historical_native_profile_remains_in_full_team_aliases(self):
        path=self.data/'fixtures.csv';frame=pd.read_csv(path,dtype=str)
        frame['home_team_id']='3294';frame['sofascore_home_team_id']='37426';frame['home_team']='Farul Constanța'
        frame.to_csv(path,index=False)
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        teams=pd.read_csv(self.data/'concordance/full/teams.csv',dtype=str,keep_default_na=False)
        row=teams[teams.sofascore_id.eq('3294')].iloc[0]
        self.assertIn('37426',json.loads(row.provider_aliases)['sofascore_id'])
        self.assertTrue(validate_bundle(self.data/'concordance')['id_requirements_met'])

    def test_provider_only_player_and_checkpoint_link_form_one_reference(self):
        self.raw['player'].append({'player_name':'Other provider name',
                                   '_native':{'fotmob_id':['200']}})
        with patch('concordance.build.observations',return_value=self.raw):rebuild(self.root,self.data,allow_unconfirmed=True)
        players=pd.read_csv(self.data/'concordance/scraped/players.csv',dtype=str,keep_default_na=False)
        self.assertEqual(len(players),1)
        self.assertEqual((players.iloc[0].sofascore_id,players.iloc[0].fotmob_id),('100','200'))

    def test_researched_history_does_not_create_provider_equivalence(self):
        history=pd.DataFrame([{'relationship_id':'history-1','from_sofascore_id':'99',
                               'from_fotmob_id':'199','to_sofascore_id':'98'}])
        with patch('concordance.build.observations',return_value=self.raw), \
             patch('concordance.build.history_frame',return_value=history):
            rebuild(self.root,self.data,allow_unconfirmed=True)
        folder=self.data/'concordance'
        registry=json.loads((folder/'alex_registry.json').read_text())
        self.assertNotIn('team|fotmob|199',registry['aliases'])
        rows=pd.read_csv(folder/'full/entities.csv',dtype=str,keep_default_na=False)
        self.assertEqual(rows.iloc[0].from_safe_for_cross_provider_joins,'False')

    def test_raw_lineups_are_indexed_even_without_a_checkpoint_or_flat_export(self):
        from tests.test_ensemble import populate
        from concordance.sources import observations
        populate(self.root,limit=1)
        for marker in self.root.glob('*/*/match_checkpoints/*/CURRENT.json'):marker.unlink()
        for marker in self.root.glob('*/*/dataset/CURRENT.json'):marker.unlink()
        result=observations(self.root,self.data,report=lambda *a,**k:None)
        self.assertIn(('sofascore','1'),result['observed'])
        self.assertTrue(any(row['_native'].get('sofascore_id') for row in result['player']))


class RawFotmobIdentityTests(unittest.TestCase):
    def test_unknown_team_zero_uses_explicit_lineup_and_conflicting_team_fails(self):
        from concordance.sources import fotmob_references
        payload={'general':{'matchId':1,'finished':True},'header':{'teams':[{'id':10},{'id':20}]},
            'content':{'lineup':{'homeTeam':{'id':10,'name':'Home','starters':[{'id':100,'name':'Player'}],'subs':[]},
                                'awayTeam':{'id':20,'name':'Away','starters':[],'subs':[]}},
                       'playerStats':{'100':{'id':100,'teamId':0,'stats':[{'stats':{
                           'minutes':{'key':'minutes_played','stat':{'value':90}}}}]}}}}
        self.assertEqual(fotmob_references(payload,'1')['home']['players'][0]['id'],100)
        payload['content']['playerStats']['100']['teamId']=20
        with self.assertRaisesRegex(ValueError,'another team'):fotmob_references(payload,'1')

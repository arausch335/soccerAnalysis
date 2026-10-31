from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from concordance.parquet import publish,validate,read_table,wide_rows,CORE_COLUMNS,EXTRA_COLUMNS,ID_COLUMNS,NAME_COLUMNS,digest
from concordance.registry import Registry
from providers.reep import install,FORMAT,_digest
from storage.pandasStore import atomic_json


class ParquetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'raw';self.dataset=self.base/'dataset'
        self.registry=Registry(self.root/'_system/identity/alex_ids.json')
        home={'sofascore_id':'1','fotmob_id':'11','whoscored_id':'111'};home.update(self.registry.assign('team',home,'rt-home'))
        away={'sofascore_id':'2','fotmob_id':'22','whoscored_id':'222'};away.update(self.registry.assign('team',away))
        bridges=[['team',p,n,'rt-home'] for p,n in [('sofascore','1'),('fotmob','11'),('whoscored','111')]]
        install(self.root,{'format':FORMAT,'version':_digest(bridges),'rows':bridges,'names':{'team':{'rt-home':'Reep Home'}}})
        self.rows=[]
        for n in range(3):
            match={'sofascore_id':str(100+n),'fotmob_id':str(200+n),'whoscored_id':str(300+n)};match.update(self.registry.assign('match',match))
            row={'entity_id':match['entity_id'],'alex_match_id':match['alex_id'],'reep_match_id':'',
                'date':f'2024-08-{10+n}','kickoff_utc':f'2024-08-{10+n}T12:30:00+00:00','season':'2024-2025',
                'competition':'Premier League','matchweek':n+1,'round_name':'Round','status':'finished' if n<2 else 'notstarted','awarded':False,
                'home_entity_id':home['entity_id'],'away_entity_id':away['entity_id'],'alex_home_team_id':home['alex_id'],'alex_away_team_id':away['alex_id'],
                'reep_home_team_id':home['reep_id'],'reep_away_team_id':'','home_team':'Home','away_team':'Away',
                'home_score':n if n<2 else None,'away_score':0 if n<2 else None,'winner_code':1 if n<2 else None,
                'has_player_stats':n==0,'is_scheduled_for_scraping':n==2,'venue':'Ground','city':'Town',
                'fotmob_fixture_check':'validated','safe_for_cross_provider_joins':True,
                'home_safe_for_cross_provider_joins':True,'away_safe_for_cross_provider_joins':True}
            for p in ('sofascore','fotmob','whoscored'):
                row[p+'_match_id']=match[p+'_id']
                for side,ref in [('home',home),('away',away)]:
                    row[p+'_'+side+'_team_id']=ref[p+'_id'];row[p+'_'+side+'_team']=p.title()+' '+side
            self.rows.append(row)
        self.registry.save()

    def write(self,**kw):return publish(self.root,self.dataset,self.rows,registry=self.registry,**kw)

    def test_chunked_core_has_only_requested_fields_and_native_types(self):
        manifest=self.write(chunk_rows=2);base=self.dataset/'concordances';validate(base)
        self.assertEqual(len(manifest['tables']['fixtures/fixtures']['files']),2)
        frame=read_table(base,'fixtures/fixtures');self.assertEqual(tuple(frame.columns),CORE_COLUMNS)
        self.assertEqual(set(c for c in frame if c.endswith('_id')),{'match_entity_id','home_team_entity_id','away_team_entity_id'})
        schema=pq.read_schema(base/'fixtures/fixtures/part-00000.parquet')
        self.assertTrue(pa.types.is_date32(schema.field('date').type));self.assertEqual(schema.field('kickoff_utc').type.tz,'UTC')
        self.assertTrue(pa.types.is_boolean(schema.field('has_player_stats').type))
        self.assertIsNone(pq.read_table(base/'fixtures/fixtures/part-00001.parquet').to_pylist()[0]['home_score'])
        self.assertEqual(frame.home_team.tolist(),['Reep Home']*3)
        self.assertFalse(list(base.rglob('*.csv')))
        self.assertEqual(tuple(read_table(base,'fixtures/extra').columns),EXTRA_COLUMNS)
        self.assertIn('fotmob_fixture_check',read_table(base,'fixtures/metadata'))
        self.assertNotIn('fotmob_fixture_check',frame)

    def test_id_tables_have_six_ids_five_names_and_use_published_name_priority(self):
        self.write();base=self.dataset/'concordances'
        teams=read_table(base,'team_ids');self.assertEqual(tuple(teams.columns),(*ID_COLUMNS,*NAME_COLUMNS))
        home=teams[teams.entity_id.eq('rt-home')].iloc[0]
        self.assertEqual(home.entity_name,'Reep Home');self.assertEqual(home.sofascore_name,'Sofascore home')
        self.assertTrue(home.alex_id.startswith('at'));self.assertTrue(teams.reep_id.isna().any())
        away=teams[teams.reep_id.isna()].iloc[0];self.assertEqual(away.entity_id,away.alex_id)
        self.assertEqual(away.entity_name,away.sofascore_name)

    def test_wide_internal_view_preserves_linkage_without_putting_it_in_main_table(self):
        self.write();rows=wide_rows(self.dataset)
        self.assertEqual(rows[0]['home_team'],'Home');self.assertEqual(rows[0]['fotmob_fixture_check'],'validated')
        self.assertEqual(rows[0]['sofascore_match_id'],'100');self.assertEqual(rows[0]['entity_id'],self.rows[0]['entity_id'])

    def test_rebuild_removes_old_chunks_and_failed_write_keeps_previous_dataset(self):
        self.write(chunk_rows=1);base=self.dataset/'concordances';original=(base/'manifest.json').read_bytes()
        with patch('concordance.parquet.pq.write_table',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):self.write(chunk_rows=2)
        self.assertEqual((base/'manifest.json').read_bytes(),original);validate(base)
        self.write(chunk_rows=100);self.assertFalse((base/'fixtures/fixtures/part-00002.parquet').exists());validate(base)

    def test_corruption_and_unlisted_chunks_are_rejected(self):
        self.write();base=self.dataset/'concordances';extra=base/'team_ids/part-99999.parquet';extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError,'unverified'):validate(base)
        extra.unlink();path=base/'match_ids/part-00000.parquet';path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):validate(base)

    def test_preview_cannot_be_imported_as_integration_confirmed(self):
        self.write()
        with self.assertRaisesRegex(ValueError,'unconfirmed'):validate(self.dataset/'concordances',require_integration=True)

    def test_new_statistics_invalidate_the_parquet_readiness_flag(self):
        from concordance.build import invalidate_bundle
        self.write(confirmed=True)
        self.assertTrue(validate(self.dataset/'concordances',require_integration=True)['ready_for_R'])
        self.assertTrue(invalidate_bundle(self.dataset,'New player statistics arrived'))
        with self.assertRaisesRegex(ValueError,'unconfirmed'):validate(self.dataset/'concordances',require_integration=True)

    def test_transfer_paths_allow_only_the_named_parquet_tables(self):
        from collection.storageTransfer import safe_path
        self.assertEqual(safe_path('concordances/fixtures/fixtures/part-00000.parquet','dataset'),
                         'concordances/fixtures/fixtures/part-00000.parquet')
        for path,kind in [('concordances/raw/part-00000.parquet','dataset'),
                          ('concordances/fixtures/fixtures/part-00000.parquet','raw'),
                          ('concordances/match_ids/../../raw.parquet','dataset')]:
            with self.assertRaises(ValueError):safe_path(path,kind)

    def test_external_r_data_directory_is_supported(self):
        target=self.base/'R/data/concordances';self.write(output=target)
        self.assertEqual(validate(target)['rows'],3)

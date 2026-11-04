"""Exercise the real R runtime against a relocated, dataset-only publication."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

import pandas as pd

from dataset_handoff import create,current,verify
from dataset_review import create_review
from collection.storageAgent import dataset_files,install_dataset
from collection.storageTransfer import FORMAT,digest
from storage.pandasStore import atomic_json
from tests import test_parquet_concordances as parquet_fixture


class RHandoffTests(unittest.TestCase):
    def setUp(self):
        self.case=parquet_fixture.ParquetTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.base=self.case.base;self.dataset=self.case.dataset

    def write(self,encoding='parquet',stats=True):
        native={'sofascore_id':'9007199254740993','fotmob_id':'777'}
        player={**native,**self.case.registry.assign('player',native),'player_name':'José, Player'}
        self.case.registry.value['aliases']['team|sofascore|9']=self.case.registry.value['aliases']['team|sofascore|1']
        for row in self.case.rows:row['baseline_provider']='sofascore'
        self.case.write(players=[player],data_format=encoding,chunk_rows=2)
        if stats:
            target=self.dataset/'raw_data/premier-league/2024-2025';target.mkdir(parents=True)
            pd.DataFrame([{'match_id':'100','player_id':native['sofascore_id'],'team_id':'1','opponent_id':'2',
                'player_name':'José, Player','competition':'Premier League','season':'2024-2025','date':'2024-08-10',
                'minutes_played':90,'xg':.3}]).to_csv(target/'player_match_stats.csv',index=False)
            pd.DataFrame([{'match_id':'100','provider':'fotmob','provider_match_id':'200','provider_team_id':'11',
                'competition':'Premier League','season':'2024-2025','field':'possession','value':52.5}]).to_csv(target/'team_match_stats.csv',index=False)
            pd.DataFrame([{'match_id':'100','competition':'Premier League','season':'2024-2025','date':'2024-08-10',
                'kickoff_utc':'2024-08-10T12:30:00+00:00','home_team_id':'1','away_team_id':'2'}]).to_csv(target/'fixtures.csv',index=False)
        create_review(self.dataset);return create(self.dataset)

    def run_r(self,code):
        executable=shutil.which('Rscript')
        if not executable:self.skipTest('Rscript unavailable')
        available=subprocess.run([executable,'-e','stopifnot(all(vapply(c("arrow","jsonlite","digest"),requireNamespace,logical(1),quietly=TRUE)))'],capture_output=True)
        if available.returncode:self.skipTest('R arrow/jsonlite/digest unavailable')
        result=subprocess.run([executable,'-e',code],cwd=self.base,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        return result

    def relocated(self):
        destination=self.base/'R project with spaces/dataset';destination.mkdir(parents=True)
        value=verify(self.dataset)
        for name in ['dataset-manifest.json',*(item['path'] for item in value['files'])]:
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(self.dataset/name,target)
        shutil.rmtree(self.dataset)
        return destination

    def test_explicit_package_command_produces_dataset_only_r_bundle(self):
        from tests.test_explicit_dataset import ExplicitDatasetTests
        from collection.datasetPackaging import package_saved
        helper=ExplicitDatasetTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.saved_match()
        self.assertEqual(package_saved(helper.root,self.dataset,report=lambda *a,**k:None),0)
        self.assertTrue(current(self.dataset));self.relocated()
        self.run_r('''root='R project with spaces/dataset';source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));x=read_dataset_table(ds,'player_match_stats')
stopifnot(nrow(x)>0,!anyNA(x$match_entity_id),!anyNA(x$player_entity_id))''')

    def test_relocated_parquet_and_csv_are_readable_with_safe_ids_and_utc_kickoffs(self):
        for encoding in ('parquet','csv'):
            with self.subTest(encoding=encoding):
                self.write(encoding);self.relocated()
                result=self.run_r('''
root='R project with spaces/dataset'
source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root))
stopifnot(nrow(ds$concordance$fixtures)==3)
stopifnot(all(format(ds$concordance$fixtures$kickoff_utc,'%H:%M:%S',tz='UTC')=='12:30:00'))
x=read_dataset_table(ds,'player_match_stats',competition='Premier League',season='2024-2025')
stopifnot(identical(x$player_id,'9007199254740993'),is.numeric(x$minutes_played),x$minutes_played==90)
stopifnot(x$player_name=='José, Player',!is.na(x$player_entity_id),!is.na(x$team_entity_id),!is.na(x$opponent_entity_id))
t=read_dataset_table(ds,'team_match_stats')
stopifnot(t$match_entity_id==x$match_entity_id,t$team_entity_id==x$team_entity_id)
f=read_dataset_table(ds,'fixtures')
stopifnot(format(f$kickoff_utc,'%H:%M:%S',tz='UTC')=='12:30:00',!is.na(f$home_team_entity_id))
strict=try(read_dataset(root,require_integration=TRUE),silent=TRUE)
stopifnot(inherits(strict,'try-error'))
missing=try(read_dataset_table(ds,'player_match_stats',season='1900-1901'),silent=TRUE)
stopifnot(inherits(missing,'try-error'))
cat('Dataset-only R handoff verified\\n')
''')
                self.assertIn('Dataset-only R handoff verified',result.stdout)
                shutil.rmtree(self.base/'R project with spaces')

    def test_historical_provider_alias_is_resolved_through_the_registry(self):
        self.write()
        path=self.dataset/'raw_data/premier-league/2024-2025/player_match_stats.csv'
        frame=pd.read_csv(path,dtype=str);frame['team_id']='9';frame.to_csv(path,index=False)
        create_review(self.dataset);create(self.dataset);self.relocated()
        self.run_r('''root='R project with spaces/dataset';source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));x=read_dataset_table(ds,'player_match_stats')
t=read_dataset_table(ds,'team_match_stats')
stopifnot(x$team_id=='9',!is.na(x$team_entity_id),x$team_entity_id==t$team_entity_id)''')

    def test_empty_season_inventory_loads_and_extra_or_corrupt_files_are_rejected(self):
        self.write(stats=False);self.relocated()
        self.run_r('''
root='R project with spaces/dataset'; source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));stopifnot(nrow(ds$tables)==0)
path=file.path(root,'concordance/fixtures/part-00000.parquet')
write('bad',file=path,append=TRUE)
stopifnot(inherits(try(verify_dataset(root),silent=TRUE),'try-error'))
''')

    def test_empty_csv_is_a_typed_zero_row_table_in_r(self):
        self.write()
        path=self.dataset/'raw_data/premier-league/2024-2025/player_match_stats.csv'
        pd.read_csv(path).iloc[:0].to_csv(path,index=False)
        create_review(self.dataset);create(self.dataset);self.relocated()
        self.run_r('''root='R project with spaces/dataset';source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));x=read_dataset_table(ds,'player_match_stats',competition='Premier League')
stopifnot(nrow(x)==0,is.character(x$match_id),is.character(x$player_id),is.character(x$match_entity_id))''')

    def test_copy_inventory_detects_missing_corrupt_and_unlisted_season_files(self):
        self.write();self.assertTrue(current(self.dataset))
        path=self.dataset/'raw_data/premier-league/2024-2025/player_match_stats.csv'
        original=path.read_bytes();path.write_bytes(original.replace(b'90,',b'80,'))
        with self.assertRaisesRegex(ValueError,'checksum'):verify(self.dataset)
        self.assertFalse(current(self.dataset));path.write_bytes(original)
        extra=path.with_name('extra.csv');extra.write_text('a\n')
        with self.assertRaisesRegex(ValueError,'inventory'):verify(self.dataset)
        extra.unlink();path.unlink()
        with self.assertRaisesRegex(ValueError,'missing'):verify(self.dataset)

    def test_receiver_rejects_missing_handoff_file_before_installing_any_public_data(self):
        self.write();files=dataset_files(self.dataset)
        files.pop('read_dataset.R')
        manifest={'format':FORMAT,'kind':'dataset','worker':'msi','files':[
            {'path':name,'size':path.stat().st_size,'sha256':digest(path)} for name,path in files.items()]}
        class Objects:
            def download(inner,sha,target):
                shutil.copyfile(next(p for p in files.values() if digest(p)==sha),target)
        output=self.base/'consumer'
        with self.assertRaisesRegex(ValueError,'missing file'):
            install_dataset(Objects(),manifest,output,self.base/'downloads')
        self.assertFalse((output/'concordance/metadata/manifest.json').exists())

    def test_mixed_columns_empty_tables_and_native_provider_selection(self):
        self.write()
        target=self.dataset/'raw_data/premier-league/2023-2024';target.mkdir(parents=True)
        pd.DataFrame([{'match_id':'201','player_id':'777','team_id':'11','competition':'Premier League',
                       'season':'2023-2024','touches':12}]).to_csv(target/'player_match_stats.csv',index=False)
        source=self.base/'source';atomic_json(source/'premier-league/2023-2024/config.json',{'id_namespace':'fotmob'})
        create_review(self.dataset);create(self.dataset,source);self.relocated()
        self.run_r('''
root='R project with spaces/dataset';source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));x=read_dataset_table(ds,'player_match_stats')
stopifnot(nrow(x)==2,setequal(x$id_namespace,c('sofascore','fotmob')))
stopifnot(length(unique(x$player_entity_id))==1,!anyNA(x$team_entity_id),sum(is.na(x$touches))==1)
''')

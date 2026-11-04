from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import dataset_builder
from collection.datasetPackaging import package_saved
from collection.storageAgent import StorageAgent,install_dataset
from collection.storageServer import Archive
from collection.workerIdentity import ensure_identity
from concordance import publication as pub
from dataset_review import create_review,load_review
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa
from tests.test_parquet_concordances import ParquetTests
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner


class ExplicitDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.output=self.base/'dataset'
        atomic_json(self.root/'.soccer-scraper.json',{'format':1});ensure_identity(self.root,'msi')

    def saved_match(self):
        source=collection_directory(self.root,'premier-league-2024-25');source.mkdir(parents=True)
        atomic_json(source/'job.json',{'competition':'Premier League','season':'2024/25','providers':['sofascore'],'spatial':[]})
        SeasonRunner({'sofascore':Sofa()},source,providers=['sofascore'],spatial=(),report=lambda _:None).run('EPL','2024',limit=1,build_csvs=False)
        return source

    def test_normal_worker_never_builds_even_with_queued_jobs(self):
        self.saved_match();archive=Archive(self.root);archive.schedule_build('premier-league/2024-2025')
        agent=StorageAgent(self.root,'msi',{'hub':'msi','consumers':['main']},dataset_root=self.output,log=lambda _:None)
        agent.server=SimpleNamespace(archive=archive)
        with patch('dataset_builder.build_dataset',side_effect=AssertionError('automatic build')),patch.object(archive,'publish',side_effect=AssertionError('automatic publication')):
            agent.maintenance_once()
        self.assertIsNone(archive.publication());self.assertEqual(archive.status()['pending_builds'],1)
        self.assertEqual(agent.maintenance_status['state'],'awaiting_package')

    def test_package_builds_reviews_publishes_and_unchanged_repeat_is_a_noop(self):
        source=self.saved_match();archive=Archive(self.root);archive.schedule_build('premier-league/2024-2025')
        before={p:p.read_bytes() for p in source.rglob('*.json')}
        self.assertEqual(package_saved(self.root,self.output,report=lambda *a,**k:None),0)
        review=load_review(self.output);self.assertEqual(review['state'],'ready')
        self.assertEqual(review['summary']['player_stat_matches'],1)
        self.assertEqual(review['summary']['fixtures'],2)
        self.assertEqual(archive.status()['pending_builds'],0)
        manifest=archive.publication();self.assertIn('coverage.json',{f['path'] for f in manifest['files']})
        self.assertTrue((self.output/'concordance/fixtures/part-00000.parquet').is_file())
        archive.schedule_build('premier-league/2024-2025')
        with patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged IDs')),patch('dataset_builder.package_collection',side_effect=AssertionError('unchanged export')),patch('dataset_review.create_review',side_effect=AssertionError('unchanged review')):
            self.assertEqual(package_saved(self.root,self.output,report=lambda *a,**k:None),0)
        self.assertEqual(archive.publication(),manifest)
        self.assertEqual(archive.status()['pending_builds'],0)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        # The same chunked package, including its review, can be installed on Main.
        class Objects:
            def download(inner,sha,target):
                from collection.storageTransfer import link_or_copy
                link_or_copy(archive.objects/sha,target)
        main=self.base/'main-dataset';install_dataset(Objects(),manifest,main,self.base/'downloads')
        self.assertEqual(load_review(main)['summary'],review['summary'])

    def fixture_helper(self):
        helper=ParquetTests();helper.setUp();self.addCleanup(helper.doCleanups);return helper

    def test_parquet_chunks_are_real_partition_files_and_old_v1_is_readable(self):
        import shutil
        helper=self.fixture_helper();manifest=helper.write(chunk_rows=2);base=pub.folder(helper.dataset)
        pieces=manifest['tables']['fixtures/fixtures']['files']
        self.assertEqual([p['path'] for p in pieces],['fixtures/part-00000.parquet','fixtures/part-00001.parquet'])
        self.assertEqual([pq.read_metadata(base/p['path']).num_rows for p in pieces],[2,1])
        self.assertTrue(pub.is_public_bundle(pub.public_paths('parquet',manifest)))
        # Convert this validated fixture to the old single-file layout, then
        # prove upgrades can still read and validate it before republishing.
        for name,info in manifest['tables'].items():
            frame=pa.concat_tables([pq.ParquetFile(base/item['path']).read() for item in info['files']])
            target=base/pub.table_path(name,'parquet',legacy=True);target.parent.mkdir(parents=True,exist_ok=True);pq.write_table(frame,target)
            shutil.rmtree((base/pub.table_path(name)).parent)
            info['files']=[{'path':target.relative_to(base).as_posix(),'sha256':pub.digest(target)}]
        manifest['format']='soccer-concordance-parquet-v1';atomic_json(pub.manifest_path(base),manifest)
        self.assertEqual(pub.validate(base)['rows'],3)
        self.assertEqual(len(pub.read_table(base,'fixtures/fixtures')),3)

    def test_review_links_qualification_and_collection_name_aliases(self):
        helper=self.fixture_helper()
        for row in helper.rows:row['competition']='Champions League Qualification'
        helper.write()
        target=helper.dataset/'raw_data/champions-league-qualifiers/2024-2025';target.mkdir(parents=True)
        pd.DataFrame([{'competition':'Champions League Qualifiers','season':'2024-2025','match_id':'100','player_id':'1'}]).to_csv(target/'player_match_stats.csv',index=False)
        review=create_review(helper.dataset)
        self.assertEqual(review['summary']['player_stat_matches'],1)
        self.assertEqual(review['seasons'][0]['player_match_stats']['matches'],1)
        self.assertEqual(review['quality']['unlinked_match_keys'],0)

    def test_review_measures_packaged_rows_separately_from_fixture_flags(self):
        helper=self.fixture_helper();helper.write();target=helper.dataset/'raw_data/premier-league/2024-2025';target.mkdir(parents=True)
        rows=[{'match_id':mid,'competition':'Premier League','season':'2024-2025','player_id':pid,'minutes_played':90} for mid,pid in [('100','1'),('100','2'),('102','3'),('999','4')]]
        pd.DataFrame(rows).to_csv(target/'player_match_stats.csv',index=False)
        review=create_review(helper.dataset)
        self.assertEqual(review['summary']['fixtures'],3)
        self.assertEqual(review['summary']['player_stat_matches'],2)
        self.assertEqual(review['summary']['player_stat_rows'],4)
        self.assertEqual(review['summary']['saved_stats_evidence'],1)
        self.assertEqual(review['quality']['unlinked_match_keys'],1)
        self.assertEqual(load_review(helper.dataset)['state'],'ready')
        (target/'player_match_stats.csv').unlink()
        self.assertEqual(load_review(helper.dataset)['state'],'stale')

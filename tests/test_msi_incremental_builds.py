"""MSI retains successful packaging work across temporary snapshot jobs."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import dataset_builder
from collection.storageAgent import StorageAgent
from collection.storageServer import Archive
from collection.paths import collection_directory
from collection.seasonRunner import SeasonRunner
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa,event


class MSIIncrementalBuildTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'data';self.output=self.base/'dataset'
        atomic_json(self.root/'.soccer-scraper.json',{'format':1})
        self.archive=Archive(self.root)
        self.agent=StorageAgent(self.root,'msi',{'hub':'msi','consumers':['main']},
                                dataset_root=self.output,log=lambda *a,**k:None)
        self.agent.server=SimpleNamespace(archive=self.archive)
        self.state=self.output/'concordance/metadata/package-state.json'

    def add_season(self,name,competition,mid):
        source=collection_directory(self.root,name)
        settings={'competition':competition,'season':'2024/2025','providers':['sofascore']}
        atomic_json(source/'config.json',settings);atomic_json(source/'job.json',settings)
        atomic_json(source/'schedule.json',{'events':[event(mid)]})
        return source

    def fixture_mode(self):
        return patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=False,
                              BUILD_TEAM_MATCH_STATS=False,BUILD_FIXTURES=True)

    def test_requeued_unchanged_job_reuses_publication_after_restart(self):
        source=self.add_season('premier-league-2024-25','Premier League',101)
        with self.fixture_mode():
            self.archive.schedule_build('premier-league/2024-2025')
            self.assertTrue(self.agent.maintenance_once())
            before=self.state.read_bytes()
            # A new agent has no in-memory state, and creates a new temporary tree.
            self.agent=StorageAgent(self.root,'msi',{'hub':'msi','consumers':['main']},dataset_root=self.output,log=lambda *a,**k:None)
            self.agent.server=SimpleNamespace(archive=self.archive)
            self.archive.schedule_build('premier-league/2024-2025')
            with patch('dataset_builder.package_collection',side_effect=AssertionError('unchanged export')), \
                 patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged ID scan')), \
                 patch.object(self.archive,'publish',side_effect=AssertionError('unchanged publication')):
                self.assertTrue(self.agent.maintenance_once())
        self.assertEqual(self.archive.build_jobs(),[])
        self.assertEqual(self.state.read_bytes(),before)
        self.assertFalse(list((self.root/'_system/storage').glob('b-*')))

    def test_next_season_reuses_other_seasons_id_observations(self):
        self.add_season('premier-league-2024-25','Premier League',101)
        second=self.add_season('bundesliga-2024-25','Bundesliga',201)
        with self.fixture_mode():
            self.archive.schedule_build('premier-league/2024-2025')
            self.assertTrue(self.agent.maintenance_once())
            self.archive.schedule_build('bundesliga/2024-2025')
            with patch('dataset_builder._source_id_rows',side_effect=AssertionError('cached ID scan')):
                self.assertTrue(self.agent.maintenance_once())
            atomic_json(second/'schedule.json',{'events':[event(201),event(202)]})
            self.archive.schedule_build('bundesliga/2024-2025')
            with patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as index:
                self.assertTrue(self.agent.maintenance_once())
        self.assertEqual(len(index.call_args_list),1)
        self.assertEqual(index.call_args.args[0].parent.name,'bundesliga')

    def test_failed_publication_does_not_advance_successful_private_checkpoint(self):
        source=self.add_season('premier-league-2024-25','Premier League',101)
        with self.fixture_mode():
            self.archive.schedule_build('premier-league/2024-2025');self.agent.maintenance_once()
            before=self.state.read_bytes()
            atomic_json(source/'schedule.json',{'events':[event(101),event(102)]})
            self.archive.schedule_build('premier-league/2024-2025')
            with patch.object(self.archive,'publish',side_effect=OSError('interrupted publication')):
                with self.assertRaises(OSError):self.agent.maintenance_once()
            self.assertEqual(self.state.read_bytes(),before)
            self.assertEqual(len(self.archive.build_jobs()),1)
            self.assertTrue(self.agent.maintenance_once())
        self.assertNotEqual(self.state.read_bytes(),before)

    def test_private_cache_swap_failure_preserves_prior_checkpoint_and_retries(self):
        from collection import datasetBuildCache
        source=self.add_season('premier-league-2024-25','Premier League',101)
        with self.fixture_mode():
            self.archive.schedule_build('premier-league/2024-2025');self.agent.maintenance_once()
            before=self.state.read_bytes()
            atomic_json(source/'schedule.json',{'events':[event(101),event(102)]})
            self.archive.schedule_build('premier-league/2024-2025')
            replace=datasetBuildCache.atomic_replace
            def sharing_error(incoming,target):
                if Path(incoming).name.startswith('.compiled-') and 'previous' not in Path(incoming).name:
                    raise PermissionError('Windows sharing conflict')
                return replace(incoming,target)
            with patch('collection.datasetBuildCache.atomic_replace',side_effect=sharing_error):
                with self.assertRaises(PermissionError):self.agent.maintenance_once()
            self.assertEqual(self.state.read_bytes(),before)
            self.assertTrue((self.output/'concordance/metadata/compiled/fixtures.csv').is_file())
            self.assertTrue(self.agent.maintenance_once())
        self.assertEqual(self.archive.build_jobs(),[])

    def test_checkpoint_exports_do_not_force_reindexing_on_the_next_snapshot(self):
        source=collection_directory(self.root,'premier-league-2024-25');source.mkdir(parents=True)
        atomic_json(source/'job.json',{'competition':'Premier League','season':'2024/25','providers':['sofascore'],'spatial':[]})
        SeasonRunner({'sofascore':Sofa()},source,providers=['sofascore'],spatial=(),report=lambda _:None).run('EPL','2024',limit=1,build_csvs=False)
        self.archive.schedule_build('premier-league/2024-2025');self.assertTrue(self.agent.maintenance_once())
        self.archive.schedule_build('premier-league/2024-2025')
        with patch('dataset_builder.package_collection',side_effect=AssertionError('unchanged raw checkpoint exported')), \
             patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged raw checkpoint reindexed')):
            self.assertTrue(self.agent.maintenance_once())
        self.assertEqual(self.archive.build_jobs(),[])

    def test_data_received_during_a_build_is_detected_by_the_next_snapshot(self):
        source=self.add_season('premier-league-2024-25','Premier League',101)
        self.archive.schedule_build('premier-league/2024-2025')
        original=dataset_builder.build_dataset
        def build_then_receive(*args,**kwargs):
            result=original(*args,**kwargs)
            atomic_json(source/'schedule.json',{'events':[event(101),event(102)]})
            self.archive.schedule_build('premier-league/2024-2025')
            return result
        with self.fixture_mode():
            with patch('dataset_builder.build_dataset',side_effect=build_then_receive):
                self.assertTrue(self.agent.maintenance_once())
            self.assertEqual(len(self.archive.build_jobs()),1)
            with patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as index:
                self.assertTrue(self.agent.maintenance_once())
            self.assertEqual(index.call_count,1)
        import pandas as pd
        self.assertEqual(len(pd.read_parquet(self.output/'concordance/fixtures.parquet')),2)

    def test_missing_archive_publication_is_restored_from_retained_outputs(self):
        self.add_season('premier-league-2024-25','Premier League',101)
        with self.fixture_mode():
            self.archive.schedule_build('premier-league/2024-2025');self.agent.maintenance_once()
            with self.archive.connect() as db:
                db.execute('DELETE FROM publication');db.commit()
            self.archive.schedule_build('premier-league/2024-2025')
            with patch('dataset_builder._source_id_rows',side_effect=AssertionError('unchanged source indexed')):
                self.assertTrue(self.agent.maintenance_once())
        self.assertIsNotNone(self.archive.publication())

    def test_incomplete_live_csv_publication_is_restored_before_parquet_build(self):
        self.add_season('premier-league-2024-25','Premier League',101)
        with self.fixture_mode(),patch.object(dataset_builder.config,'CONCORDANCE_FORMAT','csv'):
            self.archive.schedule_build('premier-league/2024-2025');self.agent.maintenance_once()
        (self.output/'concordance/match_ids.csv').unlink()
        self.add_season('bundesliga-2024-25','Bundesliga',201)
        with self.fixture_mode():
            self.archive.schedule_build('bundesliga/2024-2025')
            self.assertTrue(self.agent.maintenance_once())
        from concordance.publication import validate,folder
        result=validate(folder(self.output))
        self.assertEqual(result['format'],'soccer-concordance-parquet-v1')
        self.assertEqual(result['rows'],2)
        self.assertFalse((self.output/'concordance/match_ids.csv').exists())

    def test_background_upgrade_preserves_old_season_csvs_and_publishes_only_new_paths(self):
        self.add_season('premier-league-2024-25','Premier League',101)
        previous=self.output/'serie-a/2023-2024';previous.mkdir(parents=True)
        for name in ('fixtures','player_match_stats','team_match_stats'):
            (previous/(name+'.csv')).write_text('match_id,season\n55,2023-2024\n')
        self.archive.schedule_build('premier-league/2024-2025')
        with self.fixture_mode():self.assertTrue(self.agent.maintenance_once())
        names={row['path'] for row in self.archive.publication()['files']}
        self.assertTrue(all(name.startswith(('raw_data/','concordance/')) for name in names))
        self.assertIn('raw_data/serie-a/2023-2024/player_match_stats.csv',names)
        self.assertTrue((self.output/'raw_data/serie-a/2023-2024/player_match_stats.csv').is_file())
        self.assertTrue((self.output/'concordance/metadata/legacy/serie-a/2023-2024/player_match_stats.csv').is_file())
        self.assertFalse(previous.exists())

    def test_jobs_wait_for_saved_inputs_and_wake_without_manual_requeue(self):
        season='bundesliga/2024-2025'
        self.archive.schedule_build(season)
        with patch('collection.backgroundImports.snapshot_data',side_effect=AssertionError('missing inputs snapshotted')):
            self.assertFalse(self.agent.maintenance_once())
        self.assertEqual(self.archive.status()['pending_builds'],0)
        self.assertEqual(self.archive.status()['waiting_input_builds'],1)
        self.assertEqual(self.archive.status()['blocked_builds'],0)
        self.add_season('bundesliga-2024-25','Bundesliga',201)
        with self.fixture_mode():self.assertTrue(self.agent.maintenance_once())
        self.assertEqual(self.archive.status()['waiting_input_builds'],0)
        self.assertEqual(self.archive.diagnostics()['builds'][0]['state'],'published')


if __name__=='__main__':unittest.main()

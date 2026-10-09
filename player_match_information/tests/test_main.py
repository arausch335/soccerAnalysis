import json
from pathlib import Path
import csv
import tempfile
import unittest
from unittest.mock import call,patch
import main
import build_csvs
from collection.paths import collection_directory


class MainTests(unittest.TestCase):
    def _job(self,root,name,summary=None):
        directory=collection_directory(root,name);directory.mkdir(parents=True)
        (directory/'job.json').write_text('{}')
        if summary is not None:
            (directory/'run_summary.json').write_text(json.dumps(summary))
            discovered=summary['finished_matches_discovered']
            (directory/'schedule.json').write_text(json.dumps({'events':[{'id':i} for i in range(discovered)]}))
            complete=summary.get('complete',0);partial=summary.get('partial',0)
            with (directory/'collection.csv').open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['match_id','status'])
                writer.writeheader()
                for i in range(discovered):
                    status='complete' if i<complete else ('partial' if i<complete+partial else 'pending')
                    writer.writerow({'match_id':i,'status':status})

    def test_batch_resumes_existing_before_starting_new_and_skips_complete(self):
        complete={'selected_matches':380,'finished_matches_discovered':380,
                  'complete':380,'partial':0,'failed_or_pending':0}
        with tempfile.TemporaryDirectory() as folder:
            self._job(folder,'premier-league-2024-25',complete)
            self._job(folder,'premier-league-2025-26',{'selected_matches':380,
                'finished_matches_discovered':380,'complete':100,'partial':5,'failed_or_pending':275})
            self._job(folder,'bundesliga-2024-25')
            with patch('main.COMPETITIONS',['Premier League','Bundesliga']), \
                    patch('main.SEASONS',['2024/25','2025/26']), \
                    patch('main.collect',return_value=0) as collect:
                self.assertEqual(main.main(['--data-root',folder]),0)
            self.assertEqual(collect.call_args_list,[
                call(['resume','--name','premier-league-2025-26','--data-root',folder,
                      '--sofascore-spatial-capture','fast','--no-build-csvs']),
                call(['resume','--name','bundesliga-2024-25','--data-root',folder,
                      '--sofascore-spatial-capture','fast','--no-build-csvs']),
                call(['start','--name','bundesliga-2025-26','--data-root',folder,
                      '--competition','Bundesliga','--season','2025/2026',
                      '--sofascore-spatial-capture','fast','--no-build-csvs']),
            ])

    def test_limited_run_is_not_treated_as_a_complete_season(self):
        with tempfile.TemporaryDirectory() as folder:
            self._job(folder,'premier-league-2024-25',{'selected_matches':1,
                'finished_matches_discovered':380,'complete':1,'partial':0,'failed_or_pending':0})
            with patch('main.COMPETITIONS',['Premier League']),patch('main.SEASONS',['2024/25']), \
                    patch('main.collect',return_value=0) as collect:
                main.main(['--data-root',folder])
            collect.assert_called_once_with(['resume','--name','premier-league-2024-25',
                '--data-root',folder,'--sofascore-spatial-capture','fast','--no-build-csvs'])

    def test_finished_checkpoints_skip_even_when_summary_is_stale(self):
        stale={'selected_matches':380,'finished_matches_discovered':380,
               'complete':379,'partial':1,'failed_or_pending':0}
        with tempfile.TemporaryDirectory() as folder:
            self._job(folder,'premier-league-2024-25',stale)
            path=collection_directory(folder,'premier-league-2024-25')/'collection.csv'
            with path.open() as handle:rows=list(csv.DictReader(handle))
            for row in rows:row['status']='complete'
            with path.open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=['match_id','status']);writer.writeheader();writer.writerows(rows)
            with patch('main.COMPETITIONS',['Premier League']),patch('main.SEASONS',['2024/25']), \
                    patch('main.collect',return_value=0) as collect:
                main.main(['--data-root',folder])
            collect.assert_not_called()

    def test_one_parser_failure_does_not_stop_later_seasons(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('main.COMPETITIONS',['Bundesliga']),patch('main.SEASONS',['2024/25','2025/26']), \
                patch('main.collect',side_effect=[SystemExit(2),0]) as collect:
            self.assertEqual(main.main(['--data-root',folder]),2)
        self.assertEqual(collect.call_count,2)

    def test_all_spatial_capture_is_forwarded_to_every_batch_job(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('main.COMPETITIONS',['La Liga']),patch('main.SEASONS',['2025/26']), \
                patch('main.SOFASCORE_SPATIAL_CAPTURE','all'),patch('main.collect',return_value=0) as collect:
            main.main(['--data-root',folder])
        collect.assert_called_once_with(['start','--name','la-liga-2025-26','--data-root',folder,
            '--competition','La Liga','--season','2025/2026','--sofascore-spatial-capture','all',
            '--no-build-csvs'])

    def test_csv_build_can_be_enabled_from_main(self):
        with tempfile.TemporaryDirectory() as folder,patch('main.COMPETITIONS',['La Liga']), \
                patch('main.SEASONS',['2025/26']),patch('main.BUILD_CSVS',True), \
                patch('main.collect',return_value=0) as collect:
            main.main(['--data-root',folder])
        self.assertIn('--build-csvs',collect.call_args.args[0])

    def test_build_csvs_uses_shared_export_entry_point(self):
        with tempfile.TemporaryDirectory() as folder,patch('build_csvs.collect',return_value=0) as collect, \
                patch.multiple(build_csvs,DATA_ROOT=Path(folder),DATASET_ROOT=Path(folder)/'dataset',
                    COLLECTION_NAMES=('bundesliga-2024-25',),COMPILE_EVENTS=True,
                    REPLACE_DATASET=False,REFERENCE_CSVS_ONLY=False,
                    BUILD_PLAYER_IDS_CSV=False,BUILD_TEAM_IDS_CSV=False,
                    BUILD_MATCH_IDS_CSV=False):
            directory=collection_directory(folder,'bundesliga-2024-25');directory.mkdir(parents=True)
            (directory/'job.json').write_text('{}')
            self.assertEqual(build_csvs.main(),0)
        collect.assert_called_once_with(['export','--data-root',str(Path(folder).resolve()),
            '--name','bundesliga-2024-25','--compile-events'])

    def test_build_csvs_exports_every_saved_season_by_default(self):
        with tempfile.TemporaryDirectory() as folder,patch('build_csvs.collect',return_value=0) as collect, \
                patch.multiple(build_csvs,DATA_ROOT=Path(folder),DATASET_ROOT=Path(folder)/'dataset',
                    COLLECTION_NAMES=(),COMPILE_EVENTS=True,REPLACE_DATASET=False,
                    REFERENCE_CSVS_ONLY=False,
                    BUILD_PLAYER_IDS_CSV=False,BUILD_TEAM_IDS_CSV=False,
                    BUILD_MATCH_IDS_CSV=False):
            for name in ('premier-league-2025-26','bundesliga-2024-25'):
                directory=collection_directory(folder,name);directory.mkdir(parents=True)
                (directory/'job.json').write_text('{}')
            self.assertEqual(build_csvs.main(),0)
        self.assertEqual(collect.call_args_list,[
            call(['export','--data-root',str(Path(folder).resolve()),'--name','bundesliga-2024-25','--compile-events']),
            call(['export','--data-root',str(Path(folder).resolve()),'--name','premier-league-2025-26','--compile-events'])])

    def test_build_csvs_settings_control_events_and_id_tables(self):
        with tempfile.TemporaryDirectory() as folder,patch('build_csvs.collect',return_value=0) as collect, \
                patch('build_csvs.build_id_tables',return_value={}) as build_ids, \
                patch.multiple(build_csvs,DATA_ROOT=Path(folder),DATASET_ROOT=Path(folder)/'dataset',
                    COLLECTION_NAMES=('bundesliga-2024-25',),COMPILE_EVENTS=False,
                    REPLACE_DATASET=False,REFERENCE_CSVS_ONLY=False,
                    BUILD_PLAYER_IDS_CSV=True,BUILD_TEAM_IDS_CSV=False,
                    BUILD_MATCH_IDS_CSV=False):
            directory=collection_directory(folder,'bundesliga-2024-25');directory.mkdir(parents=True)
            (directory/'job.json').write_text('{}')
            self.assertEqual(build_csvs.main(),0)
        self.assertIn('--no-compile-events',collect.call_args.args[0])
        build_ids.assert_called_once_with(Path(folder).resolve(),(Path(folder)/'dataset').resolve(),
            build_player_ids=True,build_team_ids=False,build_match_ids=False)

    def test_build_csvs_reference_only_skips_saved_season_exports(self):
        with tempfile.TemporaryDirectory() as folder,patch('build_csvs.collect') as collect, \
                patch('build_csvs.build_id_tables',return_value={
                    'player IDs':Path(folder)/'dataset'/'player_ids.csv',
                    'match IDs':Path(folder)/'dataset'/'match_ids.csv'}) as build_ids, \
                patch.multiple(build_csvs,DATA_ROOT=Path(folder),DATASET_ROOT=Path(folder)/'dataset',
                    COLLECTION_NAMES=('does-not-need-to-exist',),COMPILE_EVENTS=False,
                    REPLACE_DATASET=False,REFERENCE_CSVS_ONLY=True,
                    BUILD_PLAYER_IDS_CSV=True,BUILD_TEAM_IDS_CSV=False,
                    BUILD_MATCH_IDS_CSV=True):
            self.assertEqual(build_csvs.main(),0)
        collect.assert_not_called()
        build_ids.assert_called_once_with(Path(folder).resolve(),(Path(folder)/'dataset').resolve(),
            build_player_ids=True,build_team_ids=False,build_match_ids=True)

    def test_fixture_name_setting_runs_only_after_team_reference_build(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('build_csvs.build_id_tables',return_value={
                    'team IDs':Path(folder)/'dataset'/'team_ids.csv'}) as build_ids, \
                patch('build_csvs.standardize_fixture_team_names') as standardize, \
                patch.multiple(build_csvs,DATA_ROOT=Path(folder),
                    DATASET_ROOT=Path(folder)/'dataset',REFERENCE_CSVS_ONLY=True,
                    BUILD_PLAYER_IDS_CSV=False,BUILD_TEAM_IDS_CSV=True,
                    BUILD_MATCH_IDS_CSV=False,STANDARDIZE_FIXTURE_TEAM_NAMES=True):
            self.assertEqual(build_csvs.main(),0)
        build_ids.assert_called_once()
        standardize.assert_called_once_with((Path(folder)/'dataset'/'fixtures.csv').resolve(),
                                            (Path(folder)/'dataset'/'team_ids.csv').resolve())

    def test_explicit_command_remains_single_collection(self):
        with patch('main.collect',return_value=0) as collect:
            main.main(['status'])
        collect.assert_called_once_with(['status','--name','premier-league-2024-25'])

    def test_explicit_selection_remains_single_collection(self):
        with patch('main.Path.exists',return_value=False),patch('main.collect',return_value=0) as collect:
            main.main(['--competition','Bundesliga','--season','2025/26'])
        collect.assert_called_once_with(['start','--name','bundesliga-2025-26',
            '--competition','Bundesliga','--season','2025/26','--sofascore-spatial-capture','fast',
            '--no-build-csvs'])

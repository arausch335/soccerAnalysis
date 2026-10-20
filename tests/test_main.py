import json
from pathlib import Path
import csv
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import call,patch
import main
from collection.paths import collection_directory


class MainTests(unittest.TestCase):
    def setUp(self):
        patcher=patch('main.ensemble_session',side_effect=lambda *a,**k:nullcontext())
        patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.dict(main.config.TRANSPORT,{'sofascore_mode':'auto'})
        patcher.start();self.addCleanup(patcher.stop)

    def _job(self,root,name,summary=None):
        directory=collection_directory(root,name);directory.mkdir(parents=True)
        (directory/'job.json').write_text(json.dumps({'workflow_version':3,'providers':['sofascore','fotmob','whoscored']}))
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

    def test_explicit_command_remains_single_collection(self):
        with patch('main.collect',return_value=0) as collect:
            main.main(['status'])
        collect.assert_called_once_with(['status','--name',main._collection_name(main.COMPETITIONS[0], main.SEASONS[0])])

    def test_explicit_selection_remains_single_collection(self):
        with patch('main.Path.exists',return_value=False),patch('main.collect',return_value=0) as collect:
            main.main(['--competition','Bundesliga','--season','2025/26'])
        collect.assert_called_once_with(['start','--name','bundesliga-2025-26',
            '--competition','Bundesliga','--season','2025/26','--sofascore-spatial-capture','fast',
            '--no-build-csvs'])

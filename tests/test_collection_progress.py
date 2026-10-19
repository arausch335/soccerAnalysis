"""Timing uses actual work and shares remaining jobs across providers/seasons."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from collection.progress import CollectionProgress, get_job, prime_job, progress_scope
from collection.whoScoredEvents import WhoScoredEventRunner
from tests.test_whoscored_events import WhoScoredOnly


class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value
    def advance(self,seconds):self.value+=seconds


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.messages=[]
        self.progress=CollectionProgress(clock=self.clock,report=self.messages.append)
        self.client=SimpleNamespace(network_requests=0)

    def job(self,key,kind='sofascore',count=4,competition='La Liga'):
        job=self.progress.job(key,kind,competition,'2023-2024')
        job.begin();job.schedule([{'id':i} for i in range(count)],mode='http')
        return job

    def scrape(self,job,mid,seconds,**options):
        job.start_game(mid,clients=(self.client,))
        self.clock.advance(seconds);self.client.network_requests+=1
        job.finish_game(clients=(self.client,),**options)

    def test_first_games_calibrate_and_overall_eta_includes_later_jobs(self):
        job=self.job('first');self.job('next',count=6)
        self.scrape(job,0,60);self.scrape(job,1,90);self.scrape(job,2,30)
        self.assertEqual(self.progress.rate('sofascore','http'),(60,3,False))
        self.assertEqual(self.progress.remaining(),(420,False))
        self.assertIn('season remaining ~1m 0s',self.messages[-1])
        self.assertIn('collection remaining ~7m 0s',self.messages[-1])
        self.assertIn('calibrating 1/3',self.messages[-3])
        self.assertIn('3 recent scraped games',self.messages[-1])

    def test_rolling_estimate_adapts_and_cache_and_failures_do_not_train(self):
        job=self.job('first',count=25)
        self.scrape(job,0,120)
        for mid in range(1,13):self.scrape(job,mid,60)
        self.assertEqual(self.progress.rate('sofascore','http'),(60,12,False))
        job.start_game(13,clients=(self.client,));self.clock.advance(1)
        job.finish_game(clients=(self.client,))
        self.scrape(job,14,1,status='failed')
        self.assertEqual(self.progress.rate('sofascore','http'),(60,12,False))
        self.assertIn('cached/skipped: 1s',self.messages[-2])

    def test_long_verification_and_user_pauses_are_excluded_from_match_eta(self):
        job=self.job('first',count=10)
        self.client.waiting_seconds=0
        job.start_game(0,clients=(self.client,))
        self.clock.advance(86400+60);self.client.network_requests+=1
        self.client.waiting_seconds=86400
        job.finish_game(clients=(self.client,))
        self.assertEqual(self.progress.rate('sofascore','http'),(60,1,False))
        self.assertIn('waiting 1d 0h 0m excluded from ETA',self.messages[-1])
        self.assertIn('season remaining ~9m 0s',self.messages[-1])

    def test_one_stuck_match_and_partial_failure_do_not_distort_typical_speed(self):
        job=self.job('first',count=10)
        self.scrape(job,0,60);self.scrape(job,1,60);self.scrape(job,2,4000000)
        self.assertEqual(self.progress.rate('sofascore','http'),(60,3,False))
        self.scrape(job,3,4000000,status='partial')
        self.assertEqual(self.progress.rate('sofascore','http'),(60,3,False))

    def test_unknown_future_schedule_and_other_provider_are_provisional(self):
        job=self.job('first')
        self.progress.job('unknown','fotmob_players','La Liga','2024-2025')
        self.scrape(job,0,60)
        self.assertEqual(self.progress.remaining(),(420,True))
        self.assertIn('(provisional)',self.messages[-1])
        self.progress.jobs[str(Path('unknown').resolve())].end(2)
        self.scrape(job,1,60)
        self.assertEqual(self.progress.remaining(),(120,False))
        self.assertIn('excludes 1 paused season',self.messages[-1])

    def test_browser_switch_learns_its_own_speed(self):
        job=self.job('first',count=5)
        self.scrape(job,0,60)
        self.scrape(job,1,180,mode='browser')
        self.assertEqual(self.progress.rate('sofascore','browser'),(180,1,False))
        self.assertEqual(self.progress.remaining(),(540,False))

    def test_no_work_is_zero_without_timing_samples_and_scope_restores(self):
        job=self.job('empty',count=0)
        self.assertEqual(self.progress.remaining(),(0,False))
        with progress_scope(self.progress):
            self.assertIs(get_job('empty','sofascore','La Liga','2023-2024'),job)
        self.assertIsNot(get_job('empty','sofascore','La Liga','2023-2024'),job)

    def test_named_resume_updates_default_selection_labels(self):
        self.progress.job('named','sofascore','Premier League','2021-2022')
        with progress_scope(self.progress):
            job=get_job('named','sofascore','La Liga','2024-2025')
        self.assertEqual((job.competition,job.season),('La Liga','2024-2025'))

    def test_saved_schedule_primes_limit_and_checkpoint_remaining(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'schedule.json').write_text(json.dumps({'events':[{'id':i} for i in range(5)]}))
            (path/'collection.csv').write_text('match_id,status\n0,complete\n1,partial\n')
            job=self.progress.job(path,'combined','La Liga','2023-2024')
            prime_job(job,path,limit=3)
            self.assertEqual((job.total,job.remaining),(3,2))

    def test_runner_reports_per_match_and_cached_resume_does_not_change_speed(self):
        with tempfile.TemporaryDirectory() as folder, progress_scope(self.progress):
            client=WhoScoredOnly();client.network_requests=0
            original=client.match_details
            def fetch(mid):
                self.clock.advance(60);client.network_requests+=1
                return original(mid)
            client.match_details=fetch
            runner=WhoScoredEventRunner(client,folder,lambda *a,**k:None)
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertEqual(self.progress.rate('whoscored_events','http'),(60,2,False))
            self.assertTrue(any('match 11 complete: 1m 0s' in line for line in self.messages))
            self.assertEqual(runner.run('EPL','2024/25'),0)
            self.assertEqual(self.progress.rate('whoscored_events','http'),(60,2,False))
            self.assertIn('cached/skipped',self.messages[-1])

    def test_real_http_client_counts_requests_but_not_saved_responses(self):
        from providers.safeClient import SafeSofaScoreClient
        with tempfile.TemporaryDirectory() as folder:
            session=Mock();session.get.return_value.status_code=200
            session.get.return_value.json.return_value={'event':{'id':1}}
            with SafeSofaScoreClient(folder,'sofascore',session=session) as client:
                client.request_gate=Mock()
                client.event(1);client.event(1)
                self.assertEqual(client.network_requests,1)
                session.get.assert_called_once()

    def test_browser_captured_response_counts_but_cache_read_does_not(self):
        import config
        from providers.browserSofaScore import BrowserSofaScoreClient
        from providers.sofaScoreTransport import SofaScoreTransport
        from tests.test_providers import event
        with tempfile.TemporaryDirectory() as folder:
            browser=BrowserSofaScoreClient(folder,config.TRANSPORT['browser'])
            response=SimpleNamespace(url='https://www.sofascore.com/api/v1/event/1',
                request=SimpleNamespace(resource_type='fetch'),status=200,headers={},
                json=lambda:{'event':event(1)})
            browser._capture(response)
            self.assertEqual(browser.event(1),{'event':event(1)})
            self.assertEqual(browser.network_requests,1)
            transport=SofaScoreTransport(folder,mode='auto',http=SimpleNamespace(network_requests=2),browser=browser)
            self.assertEqual(transport.network_requests,3)
            browser.close()



class ProgressReportingTests(unittest.TestCase):
    def test_cached_counter_remains_numeric_and_empty_active_phase_has_no_zero_eta(self):
        from collection.progress import observe_progress
        events=[];clock=Clock();progress=CollectionProgress(enabled=False,clock=clock)
        job=progress.job('cache-count','sofascore','La Liga','2023-2024')
        with observe_progress(events.append):
            job.begin();job.schedule([{'id':1},{'id':2}])
            for mid in (1,2):
                job.start_game(mid,cached=True);clock.advance(1);job.finish_game(cached=True)
            self.assertEqual(events[-1]['cached'],2)
            self.assertTrue(events[-1]['cached_match'])
            self.assertIsNone(events[-1]['eta_seconds'])
            job.end()
        self.assertTrue(events[-1]['finished']);self.assertEqual(events[-1]['eta_seconds'],0)

    def test_provider_changes_are_reported_and_latest_provider_is_retained(self):
        from collection.progress import observe_progress
        events=[];job=CollectionProgress(enabled=False).job('provider','combined','La Liga','2023-2024')
        with observe_progress(events.append):
            job.begin();job.set_provider('sofascore');job.set_provider('sofascore');job.set_provider('fotmob')
        self.assertEqual([e['provider'] for e in events],['sofascore','fotmob'])

    def test_old_complete_csv_does_not_hide_current_checkpoint_validation(self):
        from collection.progress import observe_progress
        from collection.seasonRunner import SeasonRunner
        from tests.test_providers import Sofa
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'collection.csv').write_text('match_id,status\n1,complete\n2,complete\n')
            events=[];progress=CollectionProgress(enabled=False)
            with progress_scope(progress),observe_progress(events.append):
                runner=SeasonRunner({'sofascore':Sofa()},root,providers=['sofascore'],spatial=(),report=lambda *a,**k:None)
                self.assertTrue(runner.run('EPL','2024/25').status.eq('complete').all())
            schedule=next(e for e in events if e['event']=='schedule')
            self.assertEqual(schedule['remaining'],2)
            saved=[e for e in events if e['event']=='match_saved']
            self.assertEqual([e['remaining'] for e in saved],[1,0])

if __name__=='__main__':unittest.main()

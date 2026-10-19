from copy import deepcopy
import unittest
from dashboard.jobTiming import refresh_timing


class DashboardTimingTests(unittest.TestCase):
    def runtime(self):
        key='Premier League|2022-2023|combined'
        activity=[]
        for index,duration in enumerate((10,12,14)):
            activity += [dict(event='match_started',match_id=str(index),elapsed_seconds=index*20),
                         dict(event='match_saved',match_id=str(index),elapsed_seconds=index*20+duration,status='complete',cached=False)]
        for event in activity:event.update(competition='Premier League',season='2022-2023',kind='combined')
        return {'state':'scraping','phase_stats':{key:{'total':380,'remaining':0,'scraped':9,'cached':False,'failed':0,'partial':0,'elsewhere':0,'eta_seconds':0}},'activity':activity}

    def test_live_legacy_zero_is_rebuilt_from_current_scrapes_without_mutating_snapshot(self):
        original=self.runtime();saved=deepcopy(original);value=refresh_timing(original)
        phase=next(iter(value['phase_stats'].values()))
        self.assertEqual(phase['remaining'],371);self.assertEqual(phase['eta_seconds'],371*12)
        self.assertTrue(phase['eta_provisional']);self.assertEqual(original,saved)

    def test_explicit_verification_wait_exclusion_and_cached_samples(self):
        runtime=self.runtime();phase=next(iter(runtime['phase_stats'].values()))
        phase.update(remaining=10,rate_seconds=20)
        runtime['activity'][-1]['active_match_seconds']=2
        value=refresh_timing(runtime)
        self.assertEqual(next(iter(value['phase_stats'].values()))['eta_seconds'],200)
        del phase['rate_seconds']
        runtime['activity'][-1]['cached']=True
        value=refresh_timing(runtime)
        self.assertEqual(next(iter(value['phase_stats'].values()))['eta_seconds'],110)

    def test_paused_and_unfinished_empty_phase_dont_show_zero_eta(self):
        runtime=self.runtime();runtime['state']='paused'
        self.assertIsNone(next(iter(refresh_timing(runtime)['phase_stats'].values()))['eta_seconds'])
        runtime['state']='scraping';next(iter(runtime['phase_stats'].values())).update(finished=False,remaining=0)
        self.assertIsNone(next(iter(refresh_timing(runtime)['phase_stats'].values()))['eta_seconds'])

    def test_cached_count_can_be_recovered_from_legacy_match_started_report(self):
        runtime=self.runtime();runtime['activity'][-2]['cached']=10
        phase=next(iter(refresh_timing(runtime)['phase_stats'].values()))
        self.assertEqual(phase['cached'],10);self.assertEqual(phase['remaining'],361)
        next(iter(runtime['phase_stats'].values())).update(cached=True,status='partial')
        self.assertEqual(next(iter(refresh_timing(runtime)['phase_stats'].values()))['cached'],10)

    def test_legacy_finished_phase_is_not_reconstructed_as_running(self):
        for code in (0,2):
            runtime=self.runtime();phase=next(iter(runtime['phase_stats'].values()))
            phase.update(event='job_finished',exit_code=code)
            result=next(iter(refresh_timing(runtime)['phase_stats'].values()))
            self.assertTrue(result['finished']);self.assertEqual(result['paused'],code!=0)
            self.assertEqual(result['remaining'],0)

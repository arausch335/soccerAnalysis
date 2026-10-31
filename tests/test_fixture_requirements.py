"""Played-match requirements and unpublished domestic playoff feeds."""
import csv
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from fixtures import requirements as req
from fixtures.builder import FixtureCompetition
from fixtures.dataset import _who_events,_fotmob_events,_PartialSchedule,schedules,_checkpoint,_complete_checkpoint
from fixtures.progress import Progress,validate
from providers.httpClient import ProviderRequestError
from tests.test_fixture_dataset import event,Fot

AT=datetime(2026,10,31,tzinfo=timezone.utc)
MAIN='/regions/81/tournaments/22/france-ligue-1'
SUPP='/regions/81/tournaments/660/france-ligue-1-qualification'


def who_client(start):
    client=Mock(stage_names={})
    client.seasons.side_effect=lambda path: [{'name':f'{start}/{start+1}','path':'season'}] if path==MAIN else []
    client.supplemental_path.return_value=SUPP
    client.season_stages.return_value=[1]
    client.season_events.return_value=[dict(id=300,startTimeUtc=f'{start}-09-01T12:00:00Z',
        homeTeamId=30,homeTeamName='Home',awayTeamId=40,awayTeamName='Away',homeScore=2,awayScore=1,status=6)]
    return client


class FixtureRequirementTests(unittest.TestCase):
    def test_future_missing_ids_do_not_hide_a_missing_finished_match(self):
        base=dict(competition='Ligue 1',season='2026-2027',status='finished',kickoff_utc='2026-10-01T12:00:00Z',
                  fotmob_fixture_check='validated',whoscored_fixture_check='missing')
        future={**base,'status':'notstarted','kickoff_utc':'2027-05-01T12:00:00Z'}
        cancelled={**base,'status':'canceled'}
        # A future row incorrectly labelled finished still isn't required yet.
        rows=[base,future,cancelled,{**future,'status':'finished'}]
        records=req.coverage(rows,[dict(competition='Ligue 1',season='2026-2027',provider='whoscored',status='complete')],AT)
        row,=records
        self.assertEqual((row['required_fixtures'],row['unmatched_required_fixtures']), (1,1))
        self.assertEqual((row['optional_future_fixtures'],row['excluded_unplayed_fixtures']), (2,1))
        self.assertFalse(req.summary(rows,records,AT)['all_required_provider_fixtures_validated'])
        base['whoscored_fixture_check']='validated'
        self.assertTrue(req.summary(rows,records,AT)['all_required_provider_fixtures_validated'])

    def test_reviewed_french_exceptions_do_not_remove_2023_ligue1_playoffs(self):
        for competition,start in req.NOT_HELD:
            self.assertEqual(req.domestic_phase(competition,start,at=AT),'not_applicable')
        self.assertEqual(req.domestic_phase('Ligue 1',2023,at=AT),'required')
        self.assertEqual(req.domestic_phase('Ligue 2',2024,at=AT),'required')

    def test_played_playoff_or_season_boundary_requires_unpublished_feed(self):
        baseline=[{**event(day=int(AT.timestamp())-86400),'tournament':{'name':'Promotion/Relegation Playoffs'}}]
        self.assertEqual(req.domestic_phase('Ligue 1',2026,baseline,AT),'required')
        self.assertEqual(req.domestic_phase('Ligue 1',2026,at='2027-07-01T00:00:00Z'),'required')
        records=[dict(competition='Ligue 1',season='2026-2027',provider='whoscored',status='deferred',
            scope_details={'deferred':[req.deferred_feed('Ligue 1',2026,'whoscored',660)]})]
        rows=[dict(competition='Ligue 1',season='2026-2027',competition_phase='playoffs',status='finished',
            kickoff_utc='2026-10-30T12:00:00Z')]
        self.assertEqual(req.coverage(rows,records,AT)[0]['required_status'],'partial')

    def test_historical_nonexistent_french_stage_is_not_requested(self):
        client=who_client(2022)
        with patch('fixtures.dataset._who_paths',return_value=[MAIN]):
            events=_who_events(client,FixtureCompetition('Ligue 1',34),2022,[],at=AT)
        self.assertEqual(len(events),1)
        client.supplemental_path.assert_not_called()
        self.assertEqual(client.fixture_scope['not_applicable'][0]['reason'],'division_format_transition')

    def test_future_missing_season_is_deferred_but_ambiguity_and_auth_are_errors(self):
        for failure in ('missing','ambiguous','auth','network'):
            with self.subTest(failure=failure):
                client=who_client(2026)
                if failure=='ambiguous':
                    client.seasons.side_effect=lambda path: [{'name':'2026/2027','path':'one'}]* (2 if path==SUPP else 1)
                elif failure in ('auth','network'):
                    def seasons(path):
                        if path==SUPP:raise ProviderRequestError('request failed',status_code=403 if failure=='auth' else 502)
                        return [{'name':'2026/2027','path':'season'}]
                    client.seasons.side_effect=seasons
                with patch('fixtures.dataset._who_paths',return_value=[MAIN]):
                    if failure=='missing':
                        self.assertEqual(len(_who_events(client,FixtureCompetition('Ligue 1',34),2026,[],at=AT)),1)
                        self.assertEqual(len(client.fixture_scope['deferred']),1)
                    else:
                        with self.assertRaises((ProviderRequestError,_PartialSchedule)):
                            _who_events(client,FixtureCompetition('Ligue 1',34),2026,[],at=AT)
                        self.assertNotIn('deferred',client.fixture_scope)

    def test_deferred_checkpoint_retries_online_and_expires_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);competition=FixtureCompetition('Ligue 1',34,fotmob_id=53)
            sofa=Mock();sofa.seasons.return_value={'seasons':[{'id':11,'year':'2026/2027'}]}
            sofa.iter_season_events.return_value=[event(day=int(AT.timestamp())-86400)]
            fot=Fot();fot.seasons=lambda cid:['2026/2027']
            who=who_client(2026);clients=dict(sofascore=sofa,fotmob=fot,whoscored=who)
            with patch('fixtures.dataset._who_paths',return_value=[MAIN]):
                feeds,records=schedules(root,clients,competition,2026,at=AT)
                self.assertEqual(records[-1]['status'],'deferred')
                path=_checkpoint(root,'whoscored',competition,2026)
                self.assertFalse(_complete_checkpoint(path,at=AT))
                self.assertTrue(_complete_checkpoint(path,at=AT,allow_deferred=True))
                self.assertFalse(_complete_checkpoint(path,at='2027-07-01T00:00:00Z',allow_deferred=True))
                calls=who.seasons.call_count
                schedules(root,clients,competition,2026,at=AT,offline=True)
                self.assertEqual(who.seasons.call_count,calls)
                schedules(root,clients,competition,2026,at=AT)
                self.assertGreater(who.seasons.call_count,calls)
                feeds,records=schedules(root,clients,competition,2026,at='2027-07-01T00:00:00Z')
                self.assertEqual(records[-1]['status'],'partial')

    def test_fotmob_future_playoff_season_missing_is_optional_only_until_due(self):
        comp=FixtureCompetition('Bundesliga',35,fotmob_id=54,fotmob_playoff_id=9081)
        client=Fot();client.seasons=lambda cid:['2026/2027'] if cid==54 else []
        events,errors=_fotmob_events(client,comp,2026,at=AT)
        self.assertEqual(len(events),1);self.assertFalse(errors)
        self.assertEqual(client.fixture_scope['deferred'][0]['feed'],'9081')
        events,errors=_fotmob_events(client,comp,2026,at='2027-07-01T00:00:00Z')
        self.assertTrue(errors);self.assertFalse(client.fixture_scope)

    def test_legacy_warning_reclassification_requires_successful_main_scope(self):
        base=dict(competition='Ligue 2',season='2023-2024',provider='whoscored',status='partial',
            error='_PartialSchedule: WhoScored season Ligue 2 2023/2024 unavailable or ambiguous',
            scope_details=json.dumps({'stages':[{'competition_path':'/regions/81/tournaments/37/france-ligue-2'}]}))
        result=req.coverage([], [base], AT)[0]
        self.assertEqual(result['status'],'complete');self.assertEqual(result['error'],'')
        for modified in ({'scope_details':'{}'},{'error':'WhoScored stage 1 month 202308: HTTP 502'}):
            self.assertEqual(req.coverage([], [{**base,**modified}],AT)[0]['status'],'partial')

    def test_deferred_reporting_does_not_count_as_missing_or_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Progress(Path(tmp)/'data',Path(tmp)/'ensemble',1,network=False) as progress:
                progress('provider_finished',provider='whoscored',status='deferred')
                value=validate(progress.value,progress.worker)
                self.assertEqual(value['deferred_schedules'],1)
                self.assertEqual(value['missing_schedules'],0)
                self.assertNotIn('last_issue',value)

    def test_future_only_edition_can_wait_for_missing_peer_but_not_auth_or_ambiguity(self):
        rows=[dict(competition='Supercopa de España',season='2026-2027',status='notstarted',date='2027-01-10')]
        base=dict(competition='Supercopa de España',season='2026-2027',provider='whoscored',status='missing',
            error='_UnavailableSeason: WhoScored season Supercopa de España 2026/2027 unavailable')
        result=req.coverage(rows,[base],AT)[0]
        self.assertEqual((result['status'],result['required_status'],result['required_fixtures']),('deferred','complete',0))
        self.assertEqual(req.coverage(rows,[result],'2027-01-10T00:00:00Z')[0]['required_status'],'partial')
        for error in ('ProviderRequestError: HTTP 403','ValueError: Ambiguous Supercopa season'):
            self.assertEqual(req.coverage(rows,[{**base,'error':error}],AT)[0]['status'],'missing')
        rows[0].update(status='finished',date='2026-10-01')
        self.assertEqual(req.coverage(rows,[base],AT)[0]['status'],'missing')

    def test_future_only_schedule_does_not_fabricate_peer_matches(self):
        from fixtures.builder import CUP_COMPETITIONS
        comp=next(c for c in CUP_COMPETITIONS if c.name=='Supercopa de España')
        sofa=Mock();sofa.seasons.return_value={'seasons':[dict(id=27,year='26/27')]}
        future={**event(day=int(datetime(2027,1,10,tzinfo=timezone.utc).timestamp())),'status':{'type':'notstarted'}}
        sofa.iter_season_events.return_value=[future]
        fot=Fot();fot.seasons=lambda cid:[]
        who=who_client(2026);who.seasons.return_value=[];who.seasons.side_effect=None
        with tempfile.TemporaryDirectory() as tmp,patch('fixtures.dataset._who_paths',return_value=[MAIN]):
            feeds,records=schedules(Path(tmp),dict(sofascore=sofa,fotmob=fot,whoscored=who),comp,2026,at=AT)
        self.assertEqual([r['status'] for r in records],['complete','deferred','deferred'])
        self.assertEqual(feeds['fotmob'],[]);self.assertEqual(feeds['whoscored'],[])


if __name__=='__main__':unittest.main()

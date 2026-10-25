"""Regression cases for provider scope, published stages and fixture attempts."""
from copy import deepcopy
from pathlib import Path
import csv
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from fixtures.builder import FixtureCompetition, _fotmob_event
from fixtures.dataset import (_checkpoint, _who_events, _who_paths, _PartialSchedule,
                              schedules, FORMAT, SCHEDULE_SCOPE, concord, export)
from fixtures.reconcile import reconcile
from providers.httpClient import ProviderError, ProviderRequestError
from providers.whoScoredClient import WhoScoredClient, directory_path
from storage.pandasStore import atomic_json
from tests.test_fixture_dataset import event, Sofa, Fot, Who, DAY


class PublishedDirectoryTests(unittest.TestCase):
    def test_group_stage_inventory_and_final_stage_across_format_changes(self):
        for name, tournament, season, stages in (
            ('europa-league',30,6843,[15490,15491,15492,15493,15494,15495,15498,15496,15497,15501,15500,15499,15617]),
            ('champions-league',12,6842,[15482,15483,15485,15484,15489,15487,15486,15488,15619]),
            ('conference-league',715,8696,[20105,20114,20115,20116,20117,20118,20119,20120,20267]),
            ('europa-league',30,10458,[23665,24084]),
        ):
            with self.subTest(name=name, season=season):
                base=f'regions/250/tournaments/{tournament}/seasons/{season}'
                options=''.join(f'<option value="/{base}/stages/{stage}/Show/europe-{name}">{"Final Stage" if stage==stages[-1] else "Group "+chr(65+i)}</option>' for i,stage in enumerate(stages))
                client=WhoScoredClient();client.get_text=Mock(return_value=f'<select id="stages">{options}</select>')
                self.assertEqual(client.season_stages(base+'/europe-'+name),sorted(stages))
                self.assertEqual(client.stage_names[stages[-1]],'Final Stage')
                self.assertEqual(len(client.fixture_paths),len(stages))

    def test_regular_league_uses_implicit_stage_and_ignores_other_seasons(self):
        base='regions/252/tournaments/2/seasons/6829'
        client=WhoScoredClient();client.get_text=Mock(return_value=f'''
          <a href="/{base}/stages/15151/Fixtures/england-premier-league-2017-2018">Fixtures</a>
          <a href="/regions/252/tournaments/2/seasons/9999/stages/9000/Show/england-premier-league">Other season</a>''')
        self.assertEqual(client.season_stages(base+'/england-premier-league'),[15151])

    def test_qualifying_navigation_is_in_tournament_options_not_anchors(self):
        client=WhoScoredClient();client.get_text=Mock(return_value='''<select id="tournaments">
          <option value="/Regions/250/Tournaments/759/europe-europa-league-qualification">Europa League Qualification</option>
          <option value="/Regions/250/Tournaments/757/europe-champions-league-qualification">Other</option></select>''')
        self.assertIn('/759/',client.qualification_path('europe-europa-league'))
        client.get_text=Mock(return_value='<html>loading</html>')
        with self.assertRaises(ProviderError):client.qualification_path('europe-europa-league')

    def test_directory_shell_is_refreshed_once_and_does_not_change_cache_policy(self):
        client=WhoScoredClient();client.refresh_cache=False;policies=[]
        def read(path):
            policies.append(client.refresh_cache)
            return ('<select id="seasons"><option value="/Regions/206/Tournaments/63/Seasons/6961/spain-segunda-división">2017/2018</option></select>'
                    if client.refresh_cache else '<html>loading</html>')
        client.get_text=read
        seasons=client.seasons('regions/206/tournaments/63/spain-segunda-división')
        self.assertEqual(policies,[False,True]);self.assertFalse(client.refresh_cache)
        self.assertIn('divisi%c3%b3n',seasons[0]['path'])
        client.get_text=Mock(return_value='<html>loading</html>')
        with self.assertRaises(ProviderError):client.seasons('directory')
        self.assertEqual(client.get_text.call_count,2)

    def test_unicode_slug_discovery_and_path_validation(self):
        competition=FixtureCompetition('La Liga 2',54)
        for slug in ('spain-segunda-división','spain-segunda-divisi%C3%B3n'):
            client=Mock();client.get_text.return_value=f'<select id="tournaments"><option value="/Regions/206/Tournaments/63/{slug}">Segunda</option></select>'
            self.assertEqual(_who_paths(client,competition),['regions/206/tournaments/63/spain-segunda-divisi%c3%b3n'])
        self.assertEqual(directory_path('/Regions/206/Tournaments/63/Seasons/6961'),'regions/206/tournaments/63/seasons/6961')
        for slug in ('../private','%2e%2e','abc%2fdef','abc%5cdef','abc%252fdef','abc?secret','abc#fragment'):
            with self.subTest(slug=slug),self.assertRaises(ValueError):directory_path('regions/206/tournaments/63/'+slug)

    def test_missing_one_month_retains_other_stages_and_qualifying_results(self):
        client=Who();client.stage_names={10:'Group A',11:'Final Stage',12:'Qualifying'}
        client.seasons=lambda path:[dict(name='2017/2018',path=path)]
        client.season_stages=lambda path:[12] if path=='qualifying' else [10,11]
        client.qualification_path=lambda path:'qualifying'
        calls=[]
        def read(stage,month):
            calls.append((stage,month))
            if (stage,month)==(10,'201709'):raise ProviderError('Temporary failure')
            return [dict(id=stage,startTimeUtc='2017-09-01T12:00:00Z',homeTeamId=1,homeTeamName='Home',awayTeamId=2,awayTeamName='Away',homeScore=2,awayScore=1,status=6)] if month=='201710' else []
        client.season_events=read
        competition=FixtureCompetition('Europa League',679)
        with self.assertRaises(_PartialSchedule) as raised:_who_events(client,competition,2017,[event()])
        self.assertEqual({e['id'] for e in raised.exception.events},{10,11,12})
        self.assertTrue(next(e for e in raised.exception.events if e['id']==12)['tournament']['qualificationOrPreliminary'])
        self.assertIn((12,'201706'),calls)
        self.assertIn('stage 10 month 201709',str(raised.exception))
        def blocked(stage,month):raise ProviderRequestError('Forbidden',status_code=403)
        client.season_events=Mock(side_effect=blocked)
        with self.assertRaises(ProviderRequestError):_who_events(client,competition,2017,[event()])
        self.assertEqual(client.season_events.call_count,1)

    def test_domestic_playoff_directory_uses_published_slug(self):
        client=WhoScoredClient();client.get_text=Mock(return_value='''<select id="tournaments">
            <option value="/Regions/74/Tournaments/661/france-ligue-2-qualification">Ligue 2 qualification</option></select>''')
        self.assertEqual(client.supplemental_path('directory',661),'regions/74/tournaments/661/france-ligue-2-qualification')
        with self.assertRaises(ProviderError):client.supplemental_path('directory',660)


class FixtureAttemptTests(unittest.TestCase):
    def test_cup_replacement_uses_the_played_same_round_without_deleting_other_legs(self):
        old={**event(1),'status':{'type':'postponed'},'roundInfo':{'round':2,'leg':1}}
        played={**event(2,day=DAY+86400),'roundInfo':{'round':2,'leg':1}}
        other_leg={**event(3,2,1,day=DAY+8*86400),'roundInfo':{'round':2,'leg':2}}
        kept,_=reconcile([old,played,other_leg],'sofascore','Champions League',2024)
        self.assertEqual([e['id'] for e in kept],[2,3])

    def test_a_reviewed_replay_supersedes_an_original_marked_finished(self):
        original={**event(1),'reviewedReplacement':2}
        replay=event(2,day=DAY+86400)
        kept,audit=reconcile([original,replay],'sofascore','Europa League',2024)
        self.assertEqual([e['id'] for e in kept],[2])
        self.assertEqual(audit[0]['reason'],'reviewed_abandoned_replay')
        self.assertEqual(audit[0]['original_status'],'finished')

    def test_two_legitimate_completed_matches_are_not_inferred_to_be_a_replay(self):
        kept,_=reconcile([event(1),event(2,day=DAY+86400)],'sofascore','Europa League',2024)
        self.assertEqual(len(kept),2)

    def test_postponed_replacement_preserves_audit_and_raw_data(self):
        old={**event(7438019),'status':{'type':'postponed'},'roundInfo':{'round':31}}
        new={**event(7769667,day=DAY+86400*54),'roundInfo':{'round':31}}
        raw=deepcopy([old,new]);clean,audit=reconcile(raw,'sofascore','Premier League',2017)
        self.assertEqual([e['id'] for e in clean],[7769667]);self.assertEqual(raw,[old,new])
        self.assertEqual(audit[0]['match_id'],7438019);self.assertEqual(audit[0]['canonical_match_id'],7769667)
        self.assertEqual(audit[0]['kickoff_timestamp'],old['startTimestamp'])
        self.assertEqual(audit[0]['round'],31)
        # Moved earlier rather than later is still a replacement of this round.
        old['startTimestamp']=new['startTimestamp']+86400
        self.assertEqual(len(reconcile([old,new],'sofascore','Premier League',2017)[0]),1)

    def test_cup_legs_replays_and_ambiguous_replacements_are_kept(self):
        old={**event(1),'status':{'type':'postponed'}}
        new=event(2,day=DAY+86400)
        self.assertEqual(len(reconcile([old,new],'sofascore','Europa League',2017)[0]),2)
        self.assertEqual(len(reconcile([old,new,event(3,day=DAY+2*86400)],'sofascore','Premier League',2017)[0]),3)
        old['roundInfo']={'round':1};new['roundInfo']={'round':33}
        self.assertEqual(len(reconcile([old,new],'sofascore','Premier League',2017)[0]),2)

    def test_bournemouth_luton_abandoned_game_correction_is_narrow_and_audited(self):
        abandoned=event(4193691);replay=event(4421797,day=DAY+86400*89)
        clean,audit=reconcile([abandoned,replay],'fotmob','Premier League',2023)
        self.assertEqual([e['id'] for e in clean],[4421797])
        self.assertEqual((audit[0]['original_status'],audit[0]['status']),('finished','abandoned'))
        for provider,comp,year in [('sofascore','Premier League',2023),('fotmob','Championship',2023),('fotmob','Premier League',2022)]:
            self.assertEqual(len(reconcile([abandoned,replay],provider,comp,year)[0]),2)
        clean,audit=reconcile([abandoned],'fotmob','Premier League',2023)
        self.assertEqual(clean[0]['status']['type'],'abandoned');self.assertEqual(audit[0]['canonical_match_id'],'')

    def test_fotmob_status_reason_takes_priority_over_finished_flag(self):
        for reason,state in [('Postponed','postponed'),('Abandoned','abandoned'),('Cancelled','canceled')]:
            row=Fot().season_events(47,'2010/2011')[0]
            row['status']['reason']={'short':reason}
            self.assertEqual(_fotmob_event(row,47,2010,False)['status']['type'],state)

    def test_venue_swap_only_uses_reviewed_reversed_sides_on_same_day(self):
        old={**event(1379596,67,49),'status':{'type':'postponed'}}
        new=event(1380091,49,67)
        clean,audit=reconcile([old,new],'sofascore','Premier League',2010)
        self.assertEqual(len(clean),1);self.assertEqual(audit[0]['reason'],'reviewed_venue_swap')
        new['startTimestamp']+=86400
        self.assertEqual(len(reconcile([old,new],'sofascore','Premier League',2010)[0]),2)

    def test_historical_team_profiles_are_scoped_to_season(self):
        competition=FixtureCompetition('Serie B',53)
        with tempfile.TemporaryDirectory() as root:
            feeds=[(competition,2010,{'sofascore':[event()],'fotmob':[event(200,10,20)]}),
                   (competition,2018,{'sofascore':[event(101)],'fotmob':[event(201,11,20)]})]
            rows,teams,*_=concord(feeds,root)
            self.assertEqual([r['fotmob_home_team_id'] for r in rows],['10','11'])
            self.assertTrue(all(r['home_team_identity_status']!='conflict' for r in rows))
            self.assertEqual({r['season'] for r in teams},{'2010-2011','2018-2019'})

    def test_overlapping_qualifying_feed_retains_phase_and_checks_results(self):
        from fixtures.dataset import _fotmob_events
        comp=FixtureCompetition('Champions League',7,fotmob_id=42,fotmob_qualification_id=10611)
        client=Fot()  # Both feeds deliberately publish the same native match.
        events,errors=_fotmob_events(client,comp,2010)
        self.assertFalse(errors);self.assertEqual(len(events),1)
        self.assertTrue(events[0]['tournament']['qualificationOrPreliminary'])
        def rows(cid,season):
            row=Fot().season_events(cid,season)[0]
            if cid==10611:row['status']['scoreStr']='3 - 1'
            return [row]
        client.season_events=rows
        events,errors=_fotmob_events(client,comp,2010)
        self.assertTrue(errors);self.assertEqual(len(events),1)

    def test_export_separates_request_completion_and_count_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp=FixtureCompetition('Premier League',17)
            root=Path(tmp);path=root/'dataset/fixtures.csv'
            coverage=[dict(competition=comp.name,season='2017-2018',provider='sofascore',status='complete',fixtures=2,source='test',error='')]
            old={**event(1),'status':{'type':'postponed'}}
            result=export([(comp,2017,{'sofascore':[old,event(2,day=DAY+86400)]})],coverage,root,path)
            self.assertEqual(result['rows'],1);self.assertEqual(result['fixture_attempts'],1)
            self.assertTrue(result['schedule_coverage_complete']);self.assertFalse(result['count_checks_passed'])
            with (root/'dataset/fixture_concordance/coverage.csv').open() as f:row=next(csv.DictReader(f))
            self.assertEqual((row['raw_fixtures'],row['canonical_fixtures'],row['expected_finished']),('2','1','380'))


class ScopeUpgradeTests(unittest.TestCase):
    def test_legacy_checkpoint_upgrades_once_and_offline_does_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);comp=FixtureCompetition('Premier League',17,fotmob_id=47)
            clients=dict(sofascore=Sofa(),fotmob=Fot(),whoscored=Who())
            path=_checkpoint(root,'sofascore',comp,2010)
            atomic_json(path,dict(format='soccer-fixture-schedule-v1',provider='sofascore',competition_id=17,season='2010-2011',events=[event(999)]))
            feeds,coverage=schedules(root,clients,comp,2010,offline=True)
            self.assertEqual(coverage[0]['status'],'partial');self.assertEqual(feeds['sofascore'][0]['id'],999)
            schedules(root,clients,comp,2010)
            saved=json.loads(path.read_text());self.assertEqual(saved['format'],FORMAT);self.assertEqual(saved['scope'],SCHEDULE_SCOPE)
            with patch.object(clients['sofascore'],'seasons',side_effect=AssertionError('No refetch')):
                feeds,coverage=schedules(root,clients,comp,2010,offline=True)
            self.assertEqual(coverage[0]['status'],'complete')

    def test_main_only_saved_uefa_collection_is_never_a_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);comp=FixtureCompetition('Champions League',7,fotmob_id=42)
            source=root/'champions-league/2010-2011';atomic_json(source/'schedule.json',{'events':[event()]})
            clients=dict(sofascore=Sofa(),fotmob=Fot(),whoscored=Who())
            clients['whoscored'].qualification_path=Mock(side_effect=ProviderError('No qualifying directory'))
            feeds,coverage=schedules(root,clients,comp,2010,offline=True)
            self.assertEqual(coverage[0]['status'],'partial');self.assertEqual(coverage[0]['source'],'saved_collection')
            self.assertFalse(_checkpoint(root,'sofascore',comp,2010).exists())
            clients['sofascore'].iter_season_events=lambda *a,**k:iter([event(),{**event(101),'tournament':{'qualificationOrPreliminary':True}}])
            feeds,coverage=schedules(root,clients,comp,2010)
            self.assertEqual(len(feeds['sofascore']),2);self.assertEqual(coverage[0]['status'],'complete')
            self.assertTrue(_checkpoint(root,'sofascore',comp,2010).exists())

    def test_failed_upgrade_keeps_legacy_checkpoint_and_reports_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);comp=FixtureCompetition('Premier League',17,fotmob_id=47)
            path=_checkpoint(root,'sofascore',comp,2010)
            saved=dict(format='soccer-fixture-schedule-v1',provider='sofascore',competition_id=17,season='2010-2011',events=[event(999)])
            atomic_json(path,saved)
            sofa=Sofa();sofa.seasons=Mock(side_effect=ProviderError('Unavailable'))
            feeds,coverage=schedules(root,dict(sofascore=sofa,fotmob=Fot(),whoscored=Who()),comp,2010)
            self.assertEqual(feeds['sofascore'][0]['id'],999);self.assertEqual(coverage[0]['status'],'partial')
            self.assertEqual(json.loads(path.read_text()),saved)


if __name__=='__main__':unittest.main()

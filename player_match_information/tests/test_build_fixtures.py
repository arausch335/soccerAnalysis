import csv
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from build_fixtures import FixtureCompetition, _fotmob_event, build
from dataset_manager import FIXTURE_COLUMNS


class FakeScheduleClient:
    def __init__(self):
        self.calls=[]
        self.refresh_cache=False

    def seasons(self, competition_id):
        self.calls.append(('seasons',competition_id))
        return {'seasons':[{'id':10,'year':'2010/2011'},
                           {'id':11,'year':'2011/2012'}]}

    def iter_season_events(self, competition_id, season_id, *, include_upcoming):
        self.calls.append(('events',competition_id,season_id,include_upcoming,
                           self.refresh_cache))
        base={'startTimestamp':1285000000,'season':{'id':season_id},
              'homeTeam':{'id':1,'name':'Home'},
              'awayTeam':{'id':2,'name':'Away'},
              'status':{'type':'finished'}}
        yield {**base,'id':season_id*10,'tournament':{
            'uniqueTournament':{'id':competition_id}}}
        yield {**base,'id':season_id*10+1,'tournament':{
            'uniqueTournament':{'id':competition_id},
            'qualificationOrPreliminary':True}}


class FakeFotMobClient:
    def __init__(self):
        self.calls=[]
        self.refresh_cache=False

    def seasons(self, competition_id):
        self.calls.append(('seasons',competition_id))
        return ['2010/2011']

    def season_events(self, competition_id, season):
        self.calls.append(('events',competition_id,season,self.refresh_cache))
        return [{'id':str(competition_id),'round':'1','roundName':1,
                 'home':{'id':'4','name':'Home'},
                 'away':{'id':'5','name':'Away'},
                 'status':{'utcTime':'2010-09-01T18:00:00Z',
                           'finished':True,'scoreStr':'2 - 1'}}]


class FixtureBackfillTests(unittest.TestCase):
    def test_same_schema_qualifiers_and_checkpoint_resume_without_stats(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);client=FakeScheduleClient()
            options=dict(client=client,data_root=root/'data',
                output_csv=root/'dataset'/'fixtures.csv',
                competitions=(FixtureCompetition('Champions League',7),),
                start_season=2010,end_season=2011,refresh_current=False,
                now=datetime(2026,10,7,tzinfo=timezone.utc))
            output=build(**options)
            with output.open(newline='') as stream:
                reader=csv.DictReader(stream)
                self.assertEqual(tuple(reader.fieldnames),FIXTURE_COLUMNS)
                rows=list(reader)
            self.assertEqual(len(rows),4)
            self.assertEqual(set(row['competition'] for row in rows),
                {'Champions League','Champions League Qualification'})
            self.assertEqual(set(row['season'] for row in rows),
                {'2010-2011','2011-2012'})
            self.assertEqual(sum(call[0]=='events' for call in client.calls),2)
            build(**options)
            self.assertEqual(sum(call[0]=='events' for call in client.calls),2)

    def test_failed_season_does_not_replace_combined_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'dataset'/'fixtures.csv'
            output.parent.mkdir();output.write_text('existing\n')
            client=FakeScheduleClient()
            with self.assertRaisesRegex(ValueError,'Cannot uniquely resolve'):
                build(client=client,data_root=root/'data',output_csv=output,
                    competitions=(FixtureCompetition('Championship',18),),
                    start_season=2010,end_season=2012,refresh_current=False,
                    now=datetime(2026,10,7,tzinfo=timezone.utc))
            self.assertEqual(output.read_text(),'existing\n')

    def test_current_season_refreshes_and_requests_upcoming_fixtures(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);client=FakeScheduleClient()
            build(client=client,data_root=root/'data',
                output_csv=root/'dataset'/'fixtures.csv',
                competitions=(FixtureCompetition('Premier League',17),),
                start_season=2010,end_season=2010,refresh_current=True,
                now=datetime(2010,10,7,tzinfo=timezone.utc))
            self.assertIn(('events',17,10,True,True),client.calls)
            self.assertFalse(client.refresh_cache)

    def test_fotmob_provider_includes_separate_qualifying_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);client=FakeFotMobClient()
            output=build(provider='fotmob',client=client,data_root=root/'data',
                output_csv=root/'dataset'/'fixtures.csv',
                competitions=(FixtureCompetition('Champions League',7,
                    fotmob_id=42,fotmob_qualification_id=10611),),
                start_season=2010,end_season=2010,refresh_current=False,
                now=datetime(2026,10,7,tzinfo=timezone.utc))
            with output.open(newline='') as stream:
                reader=csv.DictReader(stream)
                self.assertEqual(tuple(reader.fieldnames),FIXTURE_COLUMNS)
                rows=list(reader)
            self.assertEqual({row['competition'] for row in rows},
                {'Champions League','Champions League Qualification'})
            self.assertEqual({row['match_id'] for row in rows},{'42','10611'})
            self.assertEqual({row['home_team_id'] for row in rows},{'4'})
            self.assertEqual({row['winner_code'] for row in rows},{'1'})
            self.assertEqual({call[1] for call in client.calls if call[0]=='events'},
                             {42,10611})
            build(provider='fotmob',client=client,data_root=root/'data',
                output_csv=output,
                competitions=(FixtureCompetition('Champions League',7,
                    fotmob_id=42,fotmob_qualification_id=10611),),
                start_season=2010,end_season=2010,refresh_current=False,
                now=datetime(2026,10,7,tzinfo=timezone.utc))
            self.assertEqual(sum(call[0]=='events' for call in client.calls),2)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'PROVIDER'):
            build(provider='unknown')

    def test_fotmob_penalty_draw_has_no_invented_winner(self):
        row={'id':'100','home':{'id':'1','name':'Home'},
             'away':{'id':'2','name':'Away'},
             'status':{'utcTime':'2010-09-01T18:00:00Z','finished':True,
                       'scoreStr':'1 - 1',
                       'reason':{'shortKey':'penalties_short'}}}
        self.assertIsNone(_fotmob_event(row,10611,2010,True).get('winnerCode'))


if __name__=='__main__':
    unittest.main()

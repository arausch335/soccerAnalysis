import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

import pandas as pd

import dataset_manager


class DatasetManagerTests(unittest.TestCase):
    def test_combined_ids_are_dashed_exclude_qualifiers_and_keep_team_stints(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'data'/'premier-league'/'2024-2025'
            source.mkdir(parents=True)
            (source/'config.json').write_text(json.dumps({
                'competition':'Premier League','season':'2024/2025'}))
            main_one={'id':1,'startTimestamp':1725148800,'tournament':{},
                      'homeTeam':{'id':10,'name':'Team A'},
                      'awayTeam':{'id':30,'name':'Opponent'}}
            main_two={'id':2,'startTimestamp':1725753600,'tournament':{},
                      'homeTeam':{'id':20,'name':'Team B'},
                      'awayTeam':{'id':30,'name':'Opponent'}}
            qualifier={'id':3,'startTimestamp':1726358400,
                       'tournament':{'qualificationOrPreliminary':True},
                       'homeTeam':{'id':99,'name':'Qualifier'},
                       'awayTeam':{'id':30,'name':'Opponent'}}
            (source/'schedule.json').write_text(json.dumps(
                {'events':[main_one,main_two,qualifier]}))
            pd.DataFrame([
                {'match_id':1,'team':'Team A','player_id':7,'player_name':'Player'},
                {'match_id':2,'team':'Team B','player_id':7,'player_name':'Player'},
                {'match_id':3,'team':'Qualifier','player_id':8,'player_name':'Excluded'},
            ]).to_csv(source/'player_match_stats.csv',index=False)
            pd.DataFrame([
                {'match_id':1,'team':'Team A','player_id':7,'provider':'fotmob','provider_player_id':70},
                {'match_id':1,'team':'Team A','player_id':7,'provider':'whoscored','provider_player_id':700},
                {'match_id':2,'team':'Team B','player_id':7,'provider':'fotmob','provider_player_id':70},
                {'match_id':2,'team':'Team B','player_id':7,'provider':'whoscored','provider_player_id':700},
                {'match_id':3,'team':'Qualifier','player_id':8,'provider':'fotmob','provider_player_id':80},
            ]).to_csv(source/'identities.csv',index=False)
            snapshot=source/'dataset'/'snapshots'/('a'*32);snapshot.mkdir(parents=True)
            (source/'dataset'/'CURRENT.json').write_text(json.dumps(
                {'schema_version':1,'generation':'a'*32}))
            pd.DataFrame([
                {'id':10,'name':json.dumps('Team A')},
                {'id':20,'name':json.dumps('Team B')},
                {'id':30,'name':json.dumps('Opponent')},
            ]).to_csv(snapshot/'teams.csv',index=False)
            raw=source/'raw';raw.mkdir()
            (raw/'fotmob').mkdir();(raw/'whoscored').mkdir()
            (raw/'fotmob'/'fixtures.json').write_text(json.dumps({
                'provider':'fotmob','key':'fixtures/1/2024/2025','payload':[
                    {'id':101,'home':{'id':100,'name':'Team A'},
                     'away':{'id':300,'name':'Opponent'},
                     'status':{'utcTime':'2024-09-01T00:00:00Z','finished':True,'scoreStr':'1 - 0'}},
                    {'id':102,'home':{'id':200,'name':'Team B'},
                     'away':{'id':300,'name':'Opponent'},
                     'status':{'utcTime':'2024-09-08T00:00:00Z','finished':True,'scoreStr':'1 - 0'}},
                ]}))
            (raw/'whoscored'/'fixtures.json').write_text(json.dumps({
                'provider':'whoscored','key':'fixtures/1/202409','payload':[
                    {'id':1001,'startTimeUtc':'2024-09-01T00:00:00Z','status':6,
                     'homeTeamId':1000,'homeTeamName':'Team A',
                     'awayTeamId':3000,'awayTeamName':'Opponent'},
                    {'id':1002,'startTimeUtc':'2024-09-08T00:00:00Z','status':6,
                     'homeTeamId':2000,'homeTeamName':'Team B',
                     'awayTeamId':3000,'awayTeamName':'Opponent'},
                ]}))
            matches=[]
            for match_id,home_id,fotmob_home,whoscored_home in (
                    (1,10,100,1000),(2,20,200,2000)):
                fotmob=raw/f'fotmob-{match_id}.json'
                fotmob.write_text(json.dumps({'provider':'fotmob','payload':{'general':{
                    'homeTeam':{'id':fotmob_home},'awayTeam':{'id':300}}}}))
                whoscored=raw/f'whoscored-{match_id}.json'
                whoscored.write_text(json.dumps({'provider':'whoscored','payload':{
                    'matchCentreData':{'home':{'teamId':whoscored_home},
                                       'away':{'teamId':3000}}}}))
                metadata={'event':{'tournament':{}},'raw_response_files':{
                    'fotmob':str(fotmob.relative_to(source)),
                    'whoscored':str(whoscored.relative_to(source))}}
                matches.append({'id':match_id,'home_team_id':home_id,'away_team_id':30,
                                'metadata':json.dumps(metadata)})
            pd.DataFrame(matches).to_csv(snapshot/'matches.csv',index=False)

            (root/'dataset').mkdir(exist_ok=True)
            pd.DataFrame([
                {'home_team_id':'100','home_team':'Team A',
                 'away_team_id':'300','away_team':'Opponent'},
                {'home_team_id':'200','home_team':'Team B',
                 'away_team_id':'300','away_team':'Opponent'},
            ]).to_csv(root/'dataset'/'fixtures.csv',index=False)

            paths=dataset_manager.build_id_tables(root/'data',root/'dataset')
            players=pd.read_csv(paths['player IDs'],dtype=str,keep_default_na=False)
            teams=pd.read_csv(paths['team IDs'],dtype=str,keep_default_na=False)
            matches=pd.read_csv(paths['match IDs'],dtype=str,keep_default_na=False)
            self.assertEqual(players.season.unique().tolist(),['2024-2025'])
            self.assertEqual(players.team_name.tolist(),['Team A','Team B'])
            self.assertEqual(players.sofascore_id.tolist(),['7','7'])
            self.assertEqual(players.fotmob_id.tolist(),['70','70'])
            self.assertEqual(players.whoscored_id.tolist(),['700','700'])
            self.assertNotIn('Qualifier',teams.team_name.tolist())
            self.assertEqual(len(teams),3)
            self.assertTrue(teams.fotmob_id.is_unique)
            team_a=teams.loc[teams.team_name.eq('Team A')].iloc[0]
            self.assertEqual((team_a.sofascore_id,team_a.fotmob_id,team_a.whoscored_id),
                             ('10','100','1000'))
            self.assertEqual(matches.home_team_name.tolist(),['Team A','Team B'])
            self.assertEqual(matches.date.tolist(),['2024-09-01','2024-09-08'])
            self.assertEqual(matches.sofascore_id.tolist(),['1','2'])
            self.assertEqual(matches.fotmob_id.tolist(),['101','102'])
            self.assertEqual(matches.whoscored_id.tolist(),['1001','1002'])

            for path in paths.values():path.unlink()
            paths=dataset_manager.build_id_tables(root/'data',root/'dataset',
                build_player_ids=False,build_team_ids=False,build_match_ids=True)
            self.assertEqual(set(paths),{'match IDs'})
            self.assertTrue((root/'dataset'/'match_ids.csv').exists())
            self.assertFalse((root/'dataset'/'player_ids.csv').exists())
            self.assertFalse((root/'dataset'/'team_ids.csv').exists())

    def test_cross_season_id_backfill_requires_one_unambiguous_value(self):
        columns=('sofascore_id','fotmob_id','whoscored_id')
        rows={name:{'_ids':{column:list(values.get(column,())) for column in columns}}
              for name,values in {
                  'known':{'sofascore_id':['1'],'fotmob_id':['10'],'whoscored_id':['100']},
                  'missing':{'sofascore_id':['1']},
                  'conflict-a':{'sofascore_id':['2'],'whoscored_id':['200']},
                  'conflict-b':{'sofascore_id':['2'],'whoscored_id':['201']},
                  'conflict-missing':{'sofascore_id':['2']},
              }.items()}
        dataset_manager._backfill_unambiguous_ids(rows)
        self.assertIn('10',rows['missing']['_ids']['fotmob_id'])
        self.assertIn('100',rows['missing']['_ids']['whoscored_id'])
        self.assertEqual(rows['conflict-missing']['_ids']['whoscored_id'],[])

    def test_reviewed_provider_player_ids_reject_false_links_and_merge_duplicates(self):
        self.assertEqual(dataset_manager._reviewed_provider_player_id(
            'fotmob','1191206','1197006'),'')
        self.assertEqual(dataset_manager._reviewed_provider_player_id(
            'fotmob','1133106','1152741'),'')
        self.assertEqual(dataset_manager._reviewed_provider_player_id(
            'fotmob','1136280','1290353'),'1586939')

    def test_player_identity_audit_rejects_same_ids_under_different_names(self):
        players=pd.DataFrame([
            {'competition':'Bundesliga','season':'2025-2026','team_name':'TSG Hoffenheim',
             'player_name':'Player One','sofascore_id':'2216257',
             'fotmob_id':'1586401','whoscored_id':'551091'},
            {'competition':'Bundesliga','season':'2026-2027','team_name':'SC Paderborn 07',
             'player_name':'Player Two','sofascore_id':'2216257',
             'fotmob_id':'1586401','whoscored_id':'551091'},
        ])
        with self.assertRaisesRegex(ValueError,'Player One, Player Two'):
            dataset_manager._validate_player_identities(players)

    def test_player_identity_audit_rejects_same_name_with_overlapping_ids(self):
        players=pd.DataFrame([
            {'player_name':'Player One','sofascore_id':'1',
             'fotmob_id':'10','whoscored_id':'100'},
            {'player_name':'Player One','sofascore_id':'1',
             'fotmob_id':'11','whoscored_id':'100'},
        ])
        with self.assertRaisesRegex(ValueError,'conflicting ID sets'):
            dataset_manager._validate_player_identities(players)

    def test_player_identity_audit_allows_namesakes_with_disjoint_ids(self):
        players=pd.DataFrame([
            {'player_name':'Ben Davies','sofascore_id':'1',
             'fotmob_id':'10','whoscored_id':'100'},
            {'player_name':'Ben Davies','sofascore_id':'2',
             'fotmob_id':'20','whoscored_id':'200'},
        ])
        dataset_manager._validate_player_identities(players)

    def test_team_ids_build_with_missing_provider_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'data'/'bundesliga'/'2023-2024'
            source.mkdir(parents=True)
            (source/'config.json').write_text(json.dumps({
                'competition':'Bundesliga','season':'2023/2024'}))
            (source/'schedule.json').write_text(json.dumps({'events':[{
                'id':1,'startTimestamp':1725148800,'tournament':{},
                'homeTeam':{'id':2588,'name':'Fortuna Düsseldorf'},
                'awayTeam':{'id':30,'name':'Opponent'},
            }]}))
            (root/'dataset').mkdir(exist_ok=True)
            pd.DataFrame([{'home_team_id':'8194','home_team':'Fortuna Düsseldorf',
                           'away_team_id':'123','away_team':'Opponent'}]).to_csv(
                               root/'dataset'/'fixtures.csv',index=False)
            output=io.StringIO()
            with redirect_stdout(output):
                dataset_manager.build_id_tables(root/'data',root/'dataset',
                    build_player_ids=False,build_team_ids=True,build_match_ids=False)
            self.assertIn('1 have missing provider IDs (SofaScore: 1, WhoScored: 1)',
                          output.getvalue())
            teams=pd.read_csv(root/'dataset'/'team_ids.csv',dtype=str)
            self.assertTrue(teams.fotmob_id.is_unique)
            opponent=teams.loc[teams.fotmob_id.eq('123')].iloc[0]
            self.assertEqual(opponent.team_name,'Opponent')
            self.assertTrue(pd.isna(opponent.sofascore_id))
            self.assertTrue(pd.isna(opponent.whoscored_id))

    def test_team_reference_deduplicates_seasons_and_allows_missing_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fixtures=root/'fixtures.csv'
            pd.DataFrame([{'home_team_id':'9788','home_team':"M'gladbach",
                           'away_team_id':'300','away_team':'Opponent'}]).to_csv(
                               fixtures,index=False)
            source=pd.DataFrame([
                {'team_name':"Borussia M'gladbach",'sofascore_id':'2527',
                 'fotmob_id':'9788','whoscored_id':'978'},
                {'team_name':'Borussia Mönchengladbach','sofascore_id':'2527',
                 'fotmob_id':'9788','whoscored_id':'978'},
                {'team_name':'Opponent','sofascore_id':'',
                 'fotmob_id':'300','whoscored_id':''},
            ])
            reference=dataset_manager._team_reference(source,fixtures,
                crosswalk_path=root/'missing.json')
            self.assertEqual(len(reference),2)
            self.assertTrue(reference.fotmob_id.is_unique)
            self.assertEqual(reference.loc[reference.fotmob_id.eq('9788'),
                                           'team_name'].iloc[0],
                             'Borussia Mönchengladbach')
            self.assertTrue(pd.isna(reference.loc[reference.fotmob_id.eq('300'),
                                               'whoscored_id'].iloc[0]))

    def test_team_reference_still_rejects_conflicting_provider_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixtures=root/'fixtures.csv'
            pd.DataFrame([{'home_team_id':'123','home_team':'Opponent',
                           'away_team_id':'456','away_team':'Other'}]).to_csv(
                               fixtures,index=False)
            source=pd.DataFrame([
                {'team_name':'Opponent','sofascore_id':'10',
                 'fotmob_id':'123','whoscored_id':''},
                {'team_name':'Opponent','sofascore_id':'11',
                 'fotmob_id':'123','whoscored_id':''},
            ])
            with self.assertRaisesRegex(ValueError,'conflicting sofascore_id'):
                dataset_manager._team_reference(source,fixtures,
                    crosswalk_path=root/'missing.json')

    def test_fixture_names_use_fotmob_ids_and_leave_non_name_columns_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fixtures=root/'fixtures.csv';teams=root/'team_ids.csv'
            fixture={column:'' for column in dataset_manager.FIXTURE_COLUMNS}
            fixture.update({'match_id':'1','home_team_id':'9788',
                            'home_team':"M'gladbach",'away_team_id':'10127',
                            'away_team':'CSKA Sofia','home_score':'2'})
            pd.DataFrame([fixture]).to_csv(fixtures,index=False)
            pd.DataFrame([
                {'team_name':'Borussia Mönchengladbach','sofascore_id':'2527',
                 'fotmob_id':'9788','whoscored_id':'978'},
                {'team_name':'CSKA Sofia','sofascore_id':'',
                 'fotmob_id':'10127','whoscored_id':''},
            ]).to_csv(teams,index=False)
            before=pd.read_csv(fixtures,dtype=str,keep_default_na=False)
            dataset_manager.standardize_fixture_team_names(fixtures,teams)
            after=pd.read_csv(fixtures,dtype=str,keep_default_na=False)
            self.assertEqual(after.home_team.tolist(),['Borussia Mönchengladbach'])
            self.assertEqual(after.away_team.tolist(),['CSKA Sofia'])
            self.assertEqual(after.drop(columns=['home_team','away_team']).to_dict('records'),
                             before.drop(columns=['home_team','away_team']).to_dict('records'))

    def test_packages_nested_dataset_and_preserves_long_team_statistics(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'data'/'premier-league'/'2024-2025';source.mkdir(parents=True)
            (source/'config.json').write_text(json.dumps({
                'competition':'Premier League','season':'2024/2025'}))
            event={'id':1,'startTimestamp':1723834800,'roundInfo':{'round':1},
                   'status':{'type':'finished'},'homeTeam':{'id':10,'name':'Home'},
                   'awayTeam':{'id':20,'name':'Away'},'homeScore':{'current':2},
                   'awayScore':{'current':1},'winnerCode':1,
                   'venue':{'name':'Ground','city':{'name':'Town'}}}
            (source/'schedule.json').write_text(json.dumps({'events':[event]}))
            pd.DataFrame([{'match_id':1,'season':'Premier League 24/25','competition':'Premier League'}]).to_csv(source/'events.csv',index=False)
            pd.DataFrame([{'match_id':1,'season':'2024/2025','competition':'Premier League','player_id':7}]).to_csv(source/'player_match_stats.csv',index=False)
            pd.DataFrame([
                {'match_id':1,'season':'2024/2025','competition':'Premier League','side':'home','provider_team_id':100,'period':'All','group':'top_stats','field':'BallPossesion','value':'55'},
                {'match_id':1,'season':'2024/2025','competition':'Premier League','side':'home','provider_team_id':100,'period':'All','group':'passes','field':'BallPossesion','value':'55'},
                {'match_id':1,'season':'2024/2025','competition':'Premier League','side':'home','provider_team_id':100,'period':'FirstHalf','group':'top_stats','field':'expected_goals','value':'0.8'},
                {'match_id':1,'season':'2024/2025','competition':'Premier League','side':'away','provider_team_id':200,'period':'All','group':'top_stats','field':'BallPossesion','value':'45'},
            ]).to_csv(source/'fotmob_team_statistics.csv',index=False)
            self.assertEqual(dataset_manager.main([
                '--data-root',str(root/'data'),'--output-root',str(root/'dataset')]),0)
            destination=root/'dataset'/'premier-league'/'2024-2025'
            self.assertEqual({p.name for p in destination.iterdir()},
                {'events.csv','player_match_stats.csv','team_match_stats.csv','fixtures.csv'})
            teams=pd.read_csv(destination/'team_match_stats.csv')
            self.assertEqual(len(teams),4)
            self.assertIn('period',teams)
            self.assertIn('field',teams)
            self.assertIn('value',teams)
            self.assertNotIn('fotmob_all_ball_possesion',teams)
            fixtures=pd.read_csv(destination/'fixtures.csv')
            self.assertEqual(fixtures.iloc[0]['home_team'],'Home')
            self.assertEqual(fixtures.iloc[0]['matchweek'],1)
            for name in ('events.csv','player_match_stats.csv','team_match_stats.csv','fixtures.csv'):
                self.assertEqual(set(pd.read_csv(destination/name)['season']),{'2024-2025'})

    def test_packaged_fixtures_exclude_qualifiers_and_replace_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir()
            (source/'config.json').write_text(json.dumps({
                'competition':'Champions League','season':'2025/2026'}))
            main={'id':1,'startTimestamp':1750000000,'status':{'type':'finished'},
                  'tournament':{},'homeTeam':{'id':1,'name':'Main Home'},
                  'awayTeam':{'id':2,'name':'Main Away'}}
            qualifier={**main,'id':2,'tournament':{'qualificationOrPreliminary':True},
                       'homeTeam':{'id':3,'name':'Qualifier Home'}}
            (source/'schedule.json').write_text(json.dumps({'events':[main,qualifier]}))
            for name in ('events.csv','player_match_stats.csv','fotmob_team_statistics.csv'):
                pd.DataFrame([{'match_id':1,'season':'2025/2026'}]).to_csv(source/name,index=False)
            destination=dataset_manager.package_collection(source,root/'dataset',replace=True)
            fixtures=pd.read_csv(destination/'fixtures.csv')
            self.assertEqual(fixtures.match_id.tolist(),[1])
            (destination/'obsolete.txt').write_text('old')
            dataset_manager.package_collection(source,root/'dataset',replace=True)
            self.assertFalse((destination/'obsolete.txt').exists())

    def test_team_stream_has_identical_source_schema_across_leagues(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data'
            for name,competition,field in (
                    ('bundesliga-2024-25','Bundesliga','expected_goals'),
                    ('premier-league-2024-25','Premier League','physical_metrics_running')):
                league=name.rsplit('-',2)[0]
                source=data/league/'2024-2025';source.mkdir(parents=True)
                (source/'config.json').write_text(json.dumps({
                    'competition':competition,'season':'2024/2025'}))
                event={'id':1,'startTimestamp':1723834800,'roundInfo':{'round':1},
                       'status':{'type':'finished'},'homeTeam':{'id':10,'name':'Home'},
                       'awayTeam':{'id':20,'name':'Away'},'homeScore':{'current':0},
                       'awayScore':{'current':0}}
                (source/'schedule.json').write_text(json.dumps({'events':[event]}))
                pd.DataFrame([{'match_id':1,'season':'bad/source label','competition':competition}]).to_csv(source/'events.csv',index=False)
                pd.DataFrame([{'match_id':1,'season':'2024/2025','competition':competition}]).to_csv(source/'player_match_stats.csv',index=False)
                pd.DataFrame([{'match_id':1,'season':'2024/2025','competition':competition,
                    'side':'home','provider_team_id':100,
                    'period':'All','field':field,'value':1}]).to_csv(
                    source/'fotmob_team_statistics.csv',index=False)
            self.assertEqual(dataset_manager.main([
                '--data-root',str(data),'--output-root',str(root/'dataset')]),0)
            bundesliga=pd.read_csv(root/'dataset/bundesliga/2024-2025/team_match_stats.csv')
            premier=pd.read_csv(root/'dataset/premier-league/2024-2025/team_match_stats.csv')
            self.assertEqual(list(bundesliga.columns),list(premier.columns))
            self.assertEqual(bundesliga.loc[0,'field'],'expected_goals')
            self.assertEqual(premier.loc[0,'field'],'physical_metrics_running')


if __name__=='__main__':unittest.main()

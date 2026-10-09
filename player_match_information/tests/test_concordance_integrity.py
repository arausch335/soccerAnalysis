"""Concordances must retain unknown IDs and fail on contradictory identities."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import dataset_manager as dm
import build_csvs


class ConcordanceIntegrityTests(unittest.TestCase):
    def source(self, root):
        source=root/'data/premier-league/2024-2025'
        source.mkdir(parents=True)
        (source/'config.json').write_text(json.dumps({
            'competition':'Premier League','season':'2024/2025'}))
        (source/'schedule.json').write_text(json.dumps({'events':[{
            'id':1,'startTimestamp':1725148800,'homeTeam':{'id':10,'name':'Home'},
            'awayTeam':{'id':20,'name':'Away'}}]}))
        pd.DataFrame([{'match_id':1,'team':'Home','player_id':'9007199254740993',
                       'player_name':'Player One'}]).to_csv(source/'player_match_stats.csv',index=False)
        return source

    def metadata(self, fixtures, provider):
        fixtures.with_suffix('.metadata.json').write_text(json.dumps({
            'provider':provider,'sha256':hashlib.sha256(fixtures.read_bytes()).hexdigest()}))

    def fixture(self, path, **updates):
        row={column:'' for column in dm.FIXTURE_COLUMNS}
        row.update(match_id='101',date='2024-09-01',competition='Premier League',
                   season='2024-2025',home_team_id='10',home_team='Home',
                   away_team_id='20',away_team='Away')
        row.update(updates)
        pd.DataFrame([row]).to_csv(path,index=False)

    def test_partial_links_are_audited_in_both_directions(self):
        for rows in (
                [{'sofascore_id':'7','fotmob_id':'70','whoscored_id':''},
                 {'sofascore_id':'7','fotmob_id':'71','whoscored_id':''}],
                [{'sofascore_id':'7','fotmob_id':'70','whoscored_id':''},
                 {'sofascore_id':'8','fotmob_id':'70','whoscored_id':''}]):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,'conflicting ID sets'):
                dm._validate_provider_links(pd.DataFrame(rows),'Player')

    def test_majority_vote_cannot_hide_conflicting_ids(self):
        with self.assertRaisesRegex(ValueError,'Conflicting provider IDs'):
            dm._chosen(['70','70','71'])
        self.assertEqual(dm._chosen(['',None,'70','70.0']),'70')

    def test_conflicting_build_leaves_every_existing_concordance_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.source(root);output=root/'dataset';output.mkdir()
            pd.DataFrame([
                {'match_id':1,'team':'Home','player_id':'9007199254740993',
                 'provider':'fotmob','provider_player_id':'70'},
                {'match_id':1,'team':'Home','player_id':'9007199254740993',
                 'provider':'fotmob','provider_player_id':'71'},
            ]).to_csv(source/'identities.csv',index=False)
            for name in ('player_ids.csv','team_ids.csv','match_ids.csv'):
                (output/name).write_text('previous\n')
            with self.assertRaisesRegex(ValueError,'Conflicting provider IDs'):
                dm.build_id_tables(root/'data',output)
            for name in ('player_ids.csv','team_ids.csv','match_ids.csv'):
                self.assertEqual((output/name).read_text(),'previous\n')

    def test_missing_ids_stay_blank_and_large_ids_remain_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.source(root)
            outputs=dm.build_id_tables(root/'data',root/'dataset')
            players=pd.read_csv(outputs['player IDs'],dtype=str,keep_default_na=False)
            self.assertEqual(players.sofascore_id.tolist(),['9007199254740993'])
            self.assertEqual(players.fotmob_id.tolist(),[''])
            self.assertEqual(players.whoscored_id.tolist(),[''])
            teams=pd.read_csv(outputs['team IDs'],dtype=str,keep_default_na=False)
            self.assertEqual(set(teams.sofascore_id),{'10','20'})
            self.assertEqual(set(teams.fotmob_id),{''})

    def test_fixture_provider_controls_team_name_and_match_id_namespace(self):
        for provider in ('sofascore','fotmob'):
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);fixtures=root/'fixtures.csv';self.fixture(fixtures)
                self.metadata(fixtures,provider)
                teams=pd.DataFrame([
                    {'team_name':'Sofa Home','sofascore_id':'10','fotmob_id':'110','whoscored_id':''},
                    {'team_name':'Sofa Away','sofascore_id':'20','fotmob_id':'120','whoscored_id':''},
                    {'team_name':'Fot Home','sofascore_id':'30','fotmob_id':'10','whoscored_id':''},
                    {'team_name':'Fot Away','sofascore_id':'40','fotmob_id':'20','whoscored_id':''}])
                teams.to_csv(root/'team_ids.csv',index=False)
                dm.standardize_fixture_team_names(fixtures,root/'team_ids.csv')
                frame=pd.read_csv(fixtures)
                self.assertEqual(frame.home_team.tolist(),['Sofa Home' if provider=='sofascore' else 'Fot Home'])
                self.assertEqual(dm._fixture_provider(fixtures),provider)
                rows={};dm._include_fixture_match_ids(fixtures,rows)
                match=dm._finalize_id_rows(rows,dm.MATCH_ID_COLUMNS,['date']).iloc[0]
                self.assertEqual(match[provider+'_id'],'101')
                self.assertEqual(match[('fotmob' if provider=='sofascore' else 'sofascore')+'_id'],'')

    def test_fixture_metadata_rejects_changed_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            fixtures=Path(folder,'fixtures.csv');self.fixture(fixtures)
            self.metadata(fixtures,'sofascore')
            fixtures.write_text(fixtures.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'differs from its provider metadata'):
                dm._fixture_provider(fixtures)

    def test_fixture_only_build_retains_provider_ids_without_guessing_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'dataset';output.mkdir()
            self.fixture(output/'fixtures.csv')
            paths=dm.build_id_tables(root/'empty-data',output)
            matches=pd.read_csv(paths['match IDs'],dtype=str,keep_default_na=False)
            self.assertEqual(matches.fotmob_id.tolist(),['101'])
            self.assertEqual(matches.sofascore_id.tolist(),[''])
            self.assertEqual(matches.whoscored_id.tolist(),[''])
            self.assertTrue(pd.read_csv(paths['player IDs']).empty)

    def test_invalid_nonpositive_and_noninteger_provider_ids_fail(self):
        for value in ('0',0.0,-1.0,'-1','7.1','unknown'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'Invalid provider ID'):
                dm._identifier(value)

    def test_names_cannot_change_within_an_aggregated_player_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.source(root)
            pd.DataFrame([
                {'match_id':1,'team':'Home','player_id':7,'player_name':'Player One'},
                {'match_id':1,'team':'Home','player_id':7,'player_name':'Player Two'},
            ]).to_csv(source/'player_match_stats.csv',index=False)
            with self.assertRaisesRegex(ValueError,'assigned to different names'):
                dm.build_id_tables(root/'data',root/'dataset')

    def test_partial_team_links_can_use_a_known_sofascore_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixtures=root/'fixtures.csv'
            self.fixture(fixtures,home_team_id='123',away_team_id='456')
            sources=pd.DataFrame([
                {'team_name':'Home','sofascore_id':'10','fotmob_id':'123','whoscored_id':''},
                {'team_name':'Home','sofascore_id':'10','fotmob_id':'','whoscored_id':'100'},
            ])
            teams=dm._team_reference(sources,fixtures,crosswalk_path=root/'missing.json')
            home=teams.loc[teams.fotmob_id.eq('123')].iloc[0]
            self.assertEqual(home.whoscored_id,'100')

    def test_same_match_id_cannot_hide_conflicting_fixture_details(self):
        with tempfile.TemporaryDirectory() as folder:
            fixtures=Path(folder,'fixtures.csv');self.fixture(fixtures,home_team='Wrong Home')
            rows={}
            row=dm._id_row(rows,('Premier League','2024-2025','sofascore','1'),{
                'competition':'Premier League','season':'2024-2025','date':'2024-09-01',
                'home_team_name':'Home','away_team_name':'Away'})
            row['_ids']['sofascore_id'].append('1')
            row['_ids']['fotmob_id'].append('101')
            with self.assertRaisesRegex(ValueError,'conflicting fixtures'):
                dm._include_fixture_match_ids(fixtures,rows)

    def test_published_fotmob_register_is_not_applied_to_sofascore_fixture_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixtures=root/'fixtures.csv';self.fixture(fixtures)
            self.metadata(fixtures,'sofascore')
            with patch.multiple(build_csvs,BUILD_TEAM_IDS_CSV=True,
                    REFRESH_PUBLISHED_TEAM_IDS=True,STANDARDIZE_FIXTURE_TEAM_NAMES=False), \
                    patch('build_csvs.refresh_team_crosswalk') as refresh:
                self.assertEqual(build_csvs._build_reference_csvs(root/'data',root),2)
                refresh.assert_not_called()

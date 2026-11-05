"""Repeated FotMob group fields cannot discard a match or silently choose a value."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch

import pandas as pd
from providers.statistics import fotmob_stats,synthesize
from collection.seasonRunner import SeasonRunner
from storage.pandasStore import atomic_json
from tests.test_providers import Fot


def repeated(key,*stats):
    return {'stats':[{'title':f'Group {i}','stats':{key:{'key':key,'stat':stat}}} for i,stat in enumerate(stats)]}


class FotMobStatisticGroupTests(unittest.TestCase):
    def test_metadata_differences_do_not_make_equal_values_conflict(self):
        player=repeated('goals',{'value':1,'type':'integer','display':'1'},
                        {'value':1.0,'type':'float','display':'1.0'})
        before=deepcopy(player);raw,metrics=fotmob_stats(player)
        self.assertEqual(metrics['goals'],1);self.assertNotIn('_statistic_group_conflicts',raw)
        self.assertEqual(player,before)

    def test_partial_duplicate_adds_a_missing_total(self):
        for entries in (({'value':8},{'value':8,'total':10}),({'value':8,'total':10},{'value':8})):
            with self.subTest(entries=entries):
                raw,metrics=fotmob_stats(repeated('accurate_passes',*entries))
                self.assertEqual((metrics['passes_attempted'],metrics['passes_completed']),(10,8))
                self.assertNotIn('_statistic_group_conflicts',raw)

    def test_genuine_scalar_conflict_blanks_only_the_disputed_value(self):
        player=repeated('goals',{'value':1},{'value':2},{'value':1})
        player['stats'][0]['stats']['minutes']={'key':'minutes_played','stat':{'value':90}}
        before=deepcopy(player);raw,metrics=fotmob_stats(player)
        self.assertIsNone(metrics['goals']);self.assertEqual(metrics['minutes_played'],90)
        self.assertEqual(raw['_statistic_group_conflicts'][0]['components'],['value'])
        self.assertEqual(len(raw['_statistic_group_conflicts'][0]['groups']),3)
        self.assertEqual(player,before)

    def test_disputed_pair_component_is_not_replaced_with_another_groups_value(self):
        for stats in (({'value':8,'total':10},{'value':8,'total':12}),({'value':8,'total':12},{'value':8,'total':10})):
            _,metrics=fotmob_stats(repeated('accurate_passes',*stats))
            self.assertIsNone(metrics['passes_attempted']);self.assertEqual(metrics['passes_completed'],8)
            result=synthesize({'totalPass':11,'accuratePass':9},{'fotmob':{'metrics':metrics}},True)
            self.assertEqual((result['metrics']['passes_attempted'],result['metrics']['passes_completed']),(11,9))
            self.assertEqual(result['sources']['passes_completed'],'sofascore')

    def test_complementary_groups_cannot_create_an_impossible_action_pair(self):
        raw,metrics=fotmob_stats(repeated('accurate_passes',{'value':8},{'total':5}))
        self.assertIsNone(metrics['passes_attempted']);self.assertIsNone(metrics['passes_completed'])
        self.assertEqual(raw['_statistic_group_conflicts'][0]['components'],['value','total'])
        synthesize({}, {'fotmob':{'metrics':metrics}},True,True)

    def test_bonus_conflict_does_not_discard_consistent_fantasy_points(self):
        _,metrics=fotmob_stats(repeated('fantasy_points',{'value':8,'bonus':1},{'value':8,'bonus':2}))
        self.assertEqual(metrics['fotmob_fantasy_points'],8);self.assertIsNone(metrics['fotmob_fantasy_bonus'])

    def test_unknown_conflicting_field_is_retained_for_review(self):
        raw,metrics=fotmob_stats(repeated('future_stat',{'value':4},{'value':6}))
        self.assertEqual(raw['_statistic_group_conflicts'][0]['field'],'future_stat')
        self.assertIsNone(raw['future_stat']['value'])

    def test_offline_export_preserves_matches_and_reports_exact_conflict_without_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);fot=Fot()
            runner=SeasonRunner({'fotmob':fot},folder,providers=['fotmob'],baseline_provider='fotmob',spatial=(),report=lambda _:None)
            runner.run('Premier League','2024/25')
            path=runner.cache.path('fotmob','match/101');saved=json.loads(path.read_text())
            player=saved['payload']['content']['playerStats']['77']
            player['stats'].append({'title':'Detailed passing','stats':{'Passing':{
                'key':'accurate_passes','stat':{'value':8,'total':12}}}})
            path.write_text(json.dumps(saved));before=path.read_bytes();calls=list(fot.calls)
            runner._export(json.loads((folder/'schedule.json').read_text())['events'],compile_events=False)
            self.assertEqual(fot.calls,calls);self.assertEqual(path.read_bytes(),before)
            frame=pd.read_csv(folder/'player_match_stats.csv')
            self.assertEqual(frame.match_id.nunique(),3);self.assertEqual(len(frame),6)
            row=frame[(frame.match_id==101)&(frame.player_id==77)].iloc[0]
            self.assertTrue(pd.isna(row.passes_attempted));self.assertEqual(row.passes_completed,8)
            self.assertEqual(row.fotmob_player_stats_status,'conflicting_statistics')
            self.assertEqual(json.loads(row.fotmob_statistic_group_conflicts)[0]['field'],'accurate_passes')
            issues=pd.read_csv(folder/'data_quality.csv')
            self.assertEqual(len(issues),1);self.assertEqual(issues.iloc[0].field,'accurate_passes')
            self.assertEqual(issues.iloc[0].issue,'conflicting_statistic_groups')
            raw=pd.read_csv(folder/'fotmob_raw_player_stats.csv')
            records=raw[(raw.match_id==101)&(raw.provider_player_id==77)&(raw.field=='accurate_passes')]
            self.assertEqual(len(records),2)

    def test_package_includes_conflict_in_portable_csv_and_coverage(self):
        from collection.datasetPackaging import package_saved
        from collection.paths import collection_directory
        from dataset_handoff import verify
        from dataset_review import load_review
        import dataset_builder
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'data';output=Path(temporary)/'dataset'
            atomic_json(root/'.soccer-scraper.json',{'format':1})
            source=collection_directory(root,'premier-league-2024-25')
            fot=Fot();runner=SeasonRunner({'fotmob':fot},source,providers=['fotmob'],baseline_provider='fotmob',spatial=(),report=lambda _:None)
            runner.run('Premier League','2024/25',build_csvs=False)
            settings=json.loads((source/'config.json').read_text());settings['spatial']=[];atomic_json(source/'job.json',settings)
            path=runner.cache.path('fotmob','match/101');saved=json.loads(path.read_text())
            saved['payload']['content']['playerStats']['77']['stats'].append({'title':'Detailed','stats':{
                'Goals':{'key':'goals','stat':{'value':1}},'Passes':{'key':'accurate_passes','stat':{'value':8,'total':12}}}})
            path.write_text(json.dumps(saved));before=path.read_bytes()
            with patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=True,BUILD_TEAM_MATCH_STATS=True,BUILD_FIXTURES=True),patch('providers.httpClient.HttpClient._request',side_effect=AssertionError('network')):
                self.assertEqual(package_saved(root,output,report=lambda *a,**k:None),0)
            verify(output);review=load_review(output)
            self.assertEqual(review['package_diagnostic']['failed'],[])
            self.assertEqual(review['quality']['fotmob_statistic_conflict_rows'],1)
            example=review['quality']['fotmob_statistic_conflict_examples'][0]
            self.assertEqual(example['match_id'],'101');self.assertEqual(example['conflicts'][0]['field'],'accurate_passes')
            self.assertEqual(path.read_bytes(),before)
            csv=pd.read_csv(output/'raw_data/premier-league/2024-2025/player_match_stats.csv')
            self.assertEqual(csv.match_id.nunique(),3);self.assertIn('fotmob_statistic_group_conflicts',csv)
            if shutil.which('Rscript'):
                relocated=Path(temporary)/'R only'/'dataset';shutil.copytree(output,relocated)
                program="""root=commandArgs(TRUE)[1];source(file.path(root,'read_dataset.R'))
ds=suppressWarnings(read_dataset(root));x=read_dataset_table(ds,'player_match_stats')
row=x[x$match_id=='101' & x$player_id=='77',]
stopifnot(nrow(x)==6,nrow(row)==1,is.na(row$passes_attempted),row$passes_completed==8)
stopifnot(row$fotmob_player_stats_status=='conflicting_statistics')
detail=jsonlite::fromJSON(row$fotmob_statistic_group_conflicts)
stopifnot(detail$field=='accurate_passes')
"""
                result=subprocess.run(['Rscript','--vanilla','-e',program,str(relocated)],cwd=temporary,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

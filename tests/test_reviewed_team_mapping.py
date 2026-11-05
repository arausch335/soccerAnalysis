import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import dataset_builder
from collection.datasetPackaging import package_saved
from collection.paths import collection_directory
from concordance.publication import read_table,read_registry
from concordance.registry import Registry
from dataset_review import load_review
from dataset_handoff import verify
from providers import reep
from storage.pandasStore import atomic_json
from tests.test_providers import event


class ReviewedTeamMappingTests(unittest.TestCase):
    def test_nonnumeric_import_is_not_a_competing_reviewed_id(self):
        source=pd.DataFrame([{'team_name':'Union Brescia','sofascore_id':'1110141',
                              'fotmob_id':'189481','whoscored_id':'Union Brescia'}])
        with tempfile.TemporaryDirectory() as temporary:
            result=dataset_builder._canonical_team_reference(source,Path(temporary)/'missing.csv')
        row=result.iloc[0]
        self.assertEqual(row.whoscored_id,'8183');self.assertEqual(row.sofascore_id,'1110141')
        self.assertEqual(json.loads(row.invalid_provider_ids),{'whoscored_id':'Union Brescia'})
        self.assertEqual(row.reviewed_mapping_conflicts,'')

    def test_numeric_disagreement_is_recorded_and_peer_withheld(self):
        source=pd.DataFrame([{'team_name':'Union Brescia','sofascore_id':'1110141',
                              'fotmob_id':'189481','whoscored_id':'999'}])
        with tempfile.TemporaryDirectory() as temporary:
            row=dataset_builder._canonical_team_reference(source,Path(temporary)/'missing.csv').iloc[0]
        self.assertEqual(row.whoscored_id,'');self.assertEqual(row.sofascore_id,'1110141')
        issue=json.loads(row.reviewed_mapping_conflicts)[0]
        self.assertEqual((issue['observed_id'],issue['reviewed_id']),('999','8183'))

    def test_baseline_disagreement_keeps_observed_baseline_and_withholds_peers(self):
        source=pd.DataFrame([{'team_name':'A Club','sofascore_id':'777',
                              'fotmob_id':'189481','whoscored_id':'8183'}])
        with tempfile.TemporaryDirectory() as temporary,patch('dataset_builder._team_crosswalk',return_value={'189481':{'sofascore_id':'1110141'}}):
            row=dataset_builder._canonical_team_reference(source,Path(temporary)/'missing.csv').iloc[0]
        self.assertEqual(row.sofascore_id,'777');self.assertEqual(row.fotmob_id,'');self.assertEqual(row.whoscored_id,'')
        self.assertEqual(json.loads(row.reviewed_mapping_conflicts)[0]['provider'],'sofascore')

    def test_invalid_cached_review_value_cannot_fill_an_id_slot(self):
        source=pd.DataFrame([{'team_name':'A Club','sofascore_id':'777','fotmob_id':'222','whoscored_id':''}])
        with tempfile.TemporaryDirectory() as temporary,patch('dataset_builder._team_crosswalk',return_value={'222':{'whoscored_id':'A Club'}}):
            row=dataset_builder._canonical_team_reference(source,Path(temporary)/'missing.csv').iloc[0]
        self.assertEqual(row.whoscored_id,'')
        self.assertEqual(json.loads(row.invalid_provider_ids)['reviewed_whoscored_id'],'A Club')

    def build(self,who):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)/'data';output=Path(temporary.name)/'dataset'
        source=collection_directory(root,'premier-league-2024-25')
        atomic_json(root/'.soccer-scraper.json',{'format':1})
        settings={'competition':'Premier League','season':'2024/2025','providers':['sofascore'],'spatial':[]}
        atomic_json(source/'job.json',settings);atomic_json(source/'config.json',settings)
        fixture=event(1);fixture['homeTeam']={'id':1110141,'name':'Union Brescia'}
        fixture['awayTeam']={'id':888,'name':'Other Club'}
        atomic_json(source/'schedule.json',{'events':[fixture]})
        rows=[['team','sofascore','1110141','rt-club'],['team','fotmob','189481','rt-club'],
              ['team','whoscored','Union Brescia','rt-club'],['team','sofascore','888','rt-other'],
              ['team','whoscored','999','rt-other']]
        reep.install(root,{'format':reep.FORMAT,'version':reep._digest(rows),'rows':rows})
        registry=Registry(root/'_system/identity/alex_ids.json')
        registry.assign('team',{'sofascore_id':'1110141','fotmob_id':'189481','whoscored_id':'Union Brescia'},'rt-club')
        registry.assign('team',{'sofascore_id':'888','whoscored_id':'999'},'rt-other');registry.save()
        original=dataset_builder._source_id_rows
        def observations(source,players,teams,matches,review_root):
            original(source,players,teams,matches,review_root)
            held=teams['Premier League','2024-2025','1110141']['_ids']
            held['fotmob_id']=['189481'];held['whoscored_id']=[who]
        messages=[]
        with patch.multiple(dataset_builder.config,BUILD_PLAYER_MATCH_STATS=False,BUILD_TEAM_MATCH_STATS=False,BUILD_FIXTURES=True),patch('dataset_builder._source_id_rows',side_effect=observations):
            self.assertEqual(package_saved(root,output,report=lambda *a,**k:messages.append(str(a[0]))),0)
        verify(output)
        return output,messages

    def test_full_package_recovers_reviewed_numeric_id_and_cleans_legacy_alias(self):
        output,messages=self.build('Union Brescia')
        teams=read_table(output/'concordance','team_ids').fillna('')
        club=teams[teams.sofascore_id.eq('1110141')].iloc[0]
        self.assertEqual((club.entity_id,club.fotmob_id,club.whoscored_id),('rt-club','189481','8183'))
        registry=Registry(output/'unused.json',value=read_registry(output/'concordance'))
        self.assertEqual(registry.lookup('team','whoscored','Union Brescia'),'')
        self.assertEqual(registry.lookup('team','whoscored','8183'),'rt-club')
        self.assertEqual(load_review(output)['package_diagnostic']['failed'],[])
        self.assertTrue(load_review(output)['quality']['invalid_team_provider_ids'])

    def test_genuine_conflict_publishes_with_diagnostic_and_keeps_other_clubs_link(self):
        output,messages=self.build('999')
        teams=read_table(output/'concordance','team_ids').fillna('')
        club=teams[teams.sofascore_id.eq('1110141')].iloc[0]
        other=teams[teams.sofascore_id.eq('888')].iloc[0]
        self.assertEqual(club.entity_id,'rt-club');self.assertEqual(club.whoscored_id,'')
        self.assertEqual(other.whoscored_id,'999')
        registry=Registry(output/'unused.json',value=read_registry(output/'concordance'))
        self.assertEqual(registry.lookup('team','whoscored','999'),'rt-other')
        review=load_review(output)
        self.assertEqual(review['package_diagnostic']['failed'],[])
        self.assertEqual(review['quality']['reviewed_team_mapping_conflict_entities'],1)
        self.assertTrue(any('team identities have observed/reviewed' in line for line in messages))

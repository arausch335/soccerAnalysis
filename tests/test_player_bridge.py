from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from concordance.playerBridge import recover_lineup_aliases
from concordance.publication import read_table, read_registry, validate
from providers.cache import ResponseCache
from storage.pandasStore import atomic_json
from tests.test_parquet_concordances import ParquetTests


class PlayerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.case=ParquetTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.registry=self.case.registry;self.rows=self.case.rows
        self.players=[]
        for native,name,published in [('592560','Giorgos Masouras','rp2ea21959b1f5da'),
                                      ('369940','Giannis Masouras','')]:
            self.players.append({'sofascore_id':native,'player_name':name,
                                 **self.registry.assign('player',{'sofascore_id':native},published)})
        self.registry.save()
        self.source=self.case.root/'premier-league/2024-2025'
        atomic_json(self.source/'config.json',{'competition':'Premier League','season':'2024/2025'})
        self.cache=ResponseCache(self.source/'raw')
        self.sofa={'home':{'players':[{'player':{'id':592560,'name':'Giorgos Masouras'}}]},
                   'away':{'players':[{'player':{'id':369940,'name':'Giannis Masouras'}}]}}
        self.who={'match_id':300,'matchCentreData':{'statusCode':6,'startDate':self.rows[0]['date'],
            'home':{'teamId':111,'players':[{'playerId':366705,'name':'Giorgos Masouras','stats':{}}]},
            'away':{'teamId':222,'players':[]}}}
        self.raw()

    def raw(self,n=0,sofa=None,who=None):
        row=self.rows[n];row['whoscored_fixture_check']='validated';row['baseline_provider']='sofascore'
        sofa=deepcopy(self.sofa if sofa is None else sofa);who=deepcopy(self.who if who is None else who)
        who['match_id']=int(row['whoscored_match_id']);who['matchCentreData']['startDate']=row['date']
        self.cache.fetch('sofascore','lineups/'+row['sofascore_match_id'],lambda:sofa,refresh=True)
        self.cache.fetch('whoscored','match/'+row['whoscored_match_id'],lambda:who,refresh=True)

    def recover(self):return recover_lineup_aliases(self.case.root,self.rows,self.players,self.registry)

    def test_existing_canonical_player_is_reused_without_statistical_or_id_changes(self):
        before=deepcopy(self.registry.value);raw={p:p.read_bytes() for p in self.source.rglob('*.json')}
        result=self.recover()
        self.assertEqual(self.registry.lookup('player','whoscored','366705'),'rp2ea21959b1f5da')
        self.assertEqual(result['recovered'][0]['examples'][0]['side'],'home')
        for section in ('anchors','published','redirects'):self.assertEqual(self.registry.value[section],before[section])
        self.assertEqual({p:p.read_bytes() for p in raw},raw)
        self.assertEqual(self.recover()['recovered'],[])
        self.assertEqual(self.registry.lookup('player','sofascore','369940'),self.players[1]['entity_id'])

    def test_both_sides_use_their_own_roster_even_for_identical_names(self):
        self.sofa['away']['players'][0]['player']['name']='Giorgos Masouras'
        self.who['matchCentreData']['away']['players']=[{'playerId':777,'name':'Giorgos Masouras'}]
        self.raw();result=self.recover()
        self.assertEqual(len(result['recovered']),2)
        self.assertEqual(self.registry.lookup('player','whoscored','777'),self.players[1]['entity_id'])

    def test_a_name_on_the_wrong_team_is_not_a_global_match(self):
        self.sofa['away']['players']=self.sofa['home']['players'];self.sofa['home']['players']=[]
        self.raw();self.assertEqual(self.recover()['recovered'],[])

    def test_duplicate_names_on_either_side_are_ambiguous(self):
        for side in ('sofa','who'):
            with self.subTest(side=side):
                sofa=deepcopy(self.sofa);who=deepcopy(self.who)
                if side=='sofa':sofa['home']['players'].append({'player':{'id':369940,'name':'Giorgos Masouras'}})
                else:who['matchCentreData']['home']['players'].append({'playerId':999,'name':'Giorgos Masouras'})
                self.raw(sofa=sofa,who=who);result=self.recover()
                self.assertEqual(result['recovered'],[])
                self.assertEqual(result['conflicts'][0]['reason'],'ambiguous_lineup_name')

    def test_conflicting_match_side_date_and_unvalidated_fixtures_are_ignored(self):
        for field,value in [('whoscored_fixture_check','unverified'),('date','1999-01-01'),
                            ('home_entity_id',self.rows[0]['away_entity_id']),('entity_id','wrong-match')]:
            with self.subTest(field=field):
                old=self.rows[0][field];self.rows[0][field]=value
                self.assertEqual(self.recover()['recovered'],[]);self.rows[0][field]=old

    def test_no_player_is_created_when_missing_from_the_published_references(self):
        self.players=[];self.assertEqual(self.recover()['recovered'],[])

    def test_alias_only_fixture_is_revalidated_against_the_raw_match(self):
        row = self.rows[0]
        row['whoscored_fixture_check'] = 'bridge_only'
        del row['whoscored_match_id']
        centre = self.who['matchCentreData']
        centre['events'] = [{'teamId': 111, 'playerId': 366705}]
        for side in ('home', 'away'):
            centre[side]['scores'] = {'fulltime': 0}
        self.cache.fetch('whoscored', 'match/300', lambda: self.who, refresh=True)
        row['home_score'] = 1
        self.assertEqual(self.recover()['skipped']['fixture_validation_failed'], 1)
        row['home_score'] = 0
        self.assertEqual(len(self.recover()['recovered']), 1)

    def test_unique_shirt_and_surname_handle_different_given_names(self):
        self.sofa['home']['players'][0]['shirtNumber'] = 19
        who = self.who['matchCentreData']['home']['players'][0]
        who.update(name='Georgios Masouras', shirtNo=19)
        self.raw()
        result = self.recover()
        self.assertEqual(result['recovered'][0]['examples'][0]['method'],
                         'unique_shirt_and_shared_name_token')

    def test_unregistered_same_name_teammate_still_blocks_ambiguous_claim(self):
        self.sofa['home']['players'].append({'player': {'id': 555, 'name': 'Giorgos Masouras'}})
        self.raw()
        result = self.recover()
        self.assertFalse(result['recovered'])
        self.assertEqual(result['conflicts'][0]['reason'], 'ambiguous_lineup_name')

    def test_event_actor_missing_from_roster_can_use_same_match_name_dictionary(self):
        centre = self.who['matchCentreData']
        centre['home']['players'] = []
        centre['events'] = [{'teamId': 111, 'playerId': 366705}]
        centre['playerIdNameDictionary'] = {'366705': 'Giorgos Masouras'}
        self.raw()
        self.assertEqual(len(self.recover()['recovered']), 1)

    def test_withheld_and_previously_assigned_ids_are_preserved(self):
        token='player|whoscored|366705'
        self.registry.value['withheld']={token:'review rejected'}
        self.assertEqual(self.recover()['conflicts'][0]['reason'],'withheld_provider_mapping')
        del self.registry.value['withheld']
        self.registry.value['aliases'][token]=self.players[1]['alex_id']
        result=self.recover();self.assertEqual(result['conflicts'][0]['reason'],'existing_mapping_disagrees')
        self.assertEqual(self.registry.lookup('player','whoscored','366705'),self.players[1]['entity_id'])

    def test_rejected_pairs_cannot_be_reintroduced_by_names(self):
        with patch('concordance.playerBridge.REJECTED_PLAYER_ID_LINKS',{('whoscored','366705','592560')}):
            result=self.recover()
        self.assertEqual(result['recovered'],[])
        self.assertEqual(result['conflicts'][0]['reason'],'rejected_or_conflicting_player_link')

    def test_contradictory_fixture_claims_are_collected_before_acceptance(self):
        who=deepcopy(self.who);who['matchCentreData']['home']['players'][0]['name']='Giannis Masouras'
        sofa=deepcopy(self.sofa);sofa['home']['players']=sofa['away']['players']
        self.raw(1,sofa,who);result=self.recover()
        self.assertEqual(result['recovered'],[])
        self.assertEqual(result['conflicts'][0]['reason'],'conflicting_lineup_identities')
        self.assertFalse(self.registry.lookup('player','whoscored','366705'))

    def test_publication_exports_the_bridge_and_audit_without_changing_coverage(self):
        manifest=self.case.write(players=self.players);base=self.case.dataset/'concordance'
        validate(base)
        player=read_table(base,'player_ids').set_index('entity_id').loc[self.players[0]['entity_id']]
        self.assertEqual(player.whoscored_id,'366705')
        saved=read_registry(base);alias=saved['aliases']['player|whoscored|366705']
        self.assertEqual(saved['published'][alias],'rp2ea21959b1f5da')
        self.assertEqual(manifest['fixture_player_bridge']['recovered_aliases'],1)
        details=read_table(base,'metadata/player_ids').set_index('entity_id').loc[self.players[0]['entity_id']]
        self.assertIn('366705',details.lineup_player_aliases)
        self.assertEqual(int(read_table(base,'fixtures/fixtures').has_player_stats.sum()),1)

    def test_representative_id_and_additional_accepted_alias_are_both_retained(self):
        self.registry.assign('player',{'sofascore_id':'592560','whoscored_id':'10'},'rp2ea21959b1f5da')
        self.players[0]['whoscored_id']='10'
        self.case.write(players=self.players);base=self.case.dataset/'concordance'
        player=read_table(base,'player_ids').set_index('entity_id').loc[self.players[0]['entity_id']]
        self.assertEqual(player.whoscored_id,'10')
        details=read_table(base,'metadata/player_ids').set_index('entity_id').loc[self.players[0]['entity_id']]
        self.assertEqual(set(json.loads(details.provider_aliases)['whoscored_id']),{'10','366705'})

    def test_references_only_package_repairs_an_existing_bundle_without_season_exports(self):
        from collection.datasetPackaging import package_saved
        with patch('concordance.playerBridge.recover_lineup_aliases',return_value={'recovered':[],'conflicts':[]}):
            self.case.write(players=self.players)
        # A canonical provider-only cache need not have a combined job.json.
        self.cache.path('whoscored','match/300').unlink()
        with patch('dataset_builder.package_collection',side_effect=AssertionError('Unexpected season export')):
            self.assertEqual(package_saved(self.case.root,self.case.dataset,references_only=True,
                                           report=lambda *a,**k:None),0)
            self.raw()
            result=package_saved(self.case.root,self.case.dataset,references_only=True,report=lambda *a,**k:None)
        self.assertEqual(result,0)
        from concordance.registry import Registry
        saved=Registry(self.case.base/'unused',value=read_registry(self.case.dataset/'concordance'))
        self.assertEqual(saved.lookup('player','whoscored','366705'),'rp2ea21959b1f5da')
        review=json.loads((self.case.dataset/'coverage.json').read_text())
        self.assertEqual(review['quality']['fixture_player_bridge']['recovered_aliases'],1)

    def test_missing_or_mislabeled_raw_lineups_never_create_links(self):
        path=self.cache.path('sofascore','lineups/100');saved=json.loads(path.read_text())
        path.unlink();self.assertEqual(self.recover()['recovered'],[])
        saved['key']='lineups/999';atomic_json(path,saved)
        with self.assertRaisesRegex(ValueError,'raw response identity mismatch'):self.recover()

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from concordance.registry import Registry
from concordance.teamNamespace import repair
from dataset_builder import _fixture_team_names


class TeamNamespaceTests(unittest.TestCase):
    def rows(self):
        return ([{'entity_id':'bad','team_name':'West Ham United','fotmob_id':'37','sofascore_id':'','whoscored_id':''},
                 {'entity_id':'good','team_name':'West Ham United','sofascore_id':'37','fotmob_id':'8654','whoscored_id':'29'}],
                [{'entity_id':'good','team_name':'West Ham United','sofascore_id':'37','fotmob_id':'8654'}])

    def test_quarantines_wrong_namespace_but_keeps_good_canonical_identity(self):
        rows,fixtures=self.rows();before=deepcopy(rows)
        cleaned,audit=repair(rows,fixtures)
        self.assertEqual([r['entity_id'] for r in cleaned],['good']);self.assertEqual(len(audit),1)
        self.assertEqual(audit[0]['incorrect_id'],'37');self.assertEqual(audit[0]['verified_fotmob_id'],'8654')
        self.assertIn('namespace_quarantine',cleaned[0]);self.assertEqual(rows,before)

    def test_numeric_collision_or_name_alone_never_proves_an_identity(self):
        rows,fixtures=self.rows();rows[0]['team_name']='Different Club'
        self.assertEqual(repair(rows,fixtures)[1],[])
        rows,fixtures=self.rows();fixtures[0]['fotmob_id']='37'
        self.assertEqual(repair(rows,fixtures)[1],[])

    def test_actual_fixture_use_and_competing_evidence_block_quarantine(self):
        rows,fixtures=self.rows()
        fixtures.append({'entity_id':'bad','fotmob_id':'37','team_name':'West Ham United'})
        self.assertEqual(repair(rows,fixtures)[1],[])
        rows,fixtures=self.rows();fixtures.append({'entity_id':'other','sofascore_id':'37','fotmob_id':'123','team_name':'West Ham United'})
        self.assertEqual(repair(rows,fixtures)[1],[])

    def test_wrong_alias_is_withheld_without_redirecting_published_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry=Registry(Path(temporary)/'registry.json')
            good=registry.assign('team',{'sofascore_id':'37','fotmob_id':'8654'},'rt-good')
            bad=registry.assign('team',{'fotmob_id':'37'},'rt-old-duplicate')
            rows,fixtures=self.rows();rows[0]['entity_id']=bad['entity_id'];rows[1]['entity_id']=good['entity_id'];fixtures[0]['entity_id']=good['entity_id']
            before=deepcopy(registry.value['redirects']);repair(rows,fixtures,registry)
            self.assertEqual(registry.lookup('team','fotmob','37'),'')
            self.assertEqual(registry.lookup('team','fotmob','8654'),'rt-good')
            self.assertEqual(registry.lookup('team','sofascore','37'),'rt-good')
            self.assertEqual(registry.value['redirects'],before)

    def test_sofa_reviewed_mappings_are_not_applied_to_fotmob_baseline_numbers(self):
        import json
        import dataset_builder
        from unittest.mock import patch
        from storage.pandasStore import atomic_json
        from tests.test_providers import event
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)
            atomic_json(source/'config.json',{'competition':'Premier League','season':'2024/2025','baseline_provider':'fotmob'})
            row=event(1);row['homeTeam']={'id':37,'name':'Unrelated FotMob Club'}
            atomic_json(source/'schedule.json',{'events':[row]})
            players,teams,matches={},{},{}
            with patch.object(dataset_builder,'REVIEWED_TEAM_IDS',{('fotmob','37'):'8654'}):
                dataset_builder._source_id_rows(source,players,teams,matches)
            self.assertEqual(teams['Premier League','2024-2025','37']['_ids']['fotmob_id'],['37'])

    def test_untyped_sofa_fixture_columns_are_never_assumed_to_be_fotmob(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'fixtures.csv'
            path.write_text('home_team_id,home_team,away_team_id,away_team\n37,West Ham United,50,Brentford\n')
            self.assertEqual(_fixture_team_names(path),{})
            path.write_text('home_team_id,home_team,away_team_id,away_team,baseline_provider\n8654,West Ham United,9937,Brentford,fotmob\n')
            self.assertEqual(_fixture_team_names(path),{'8654':'West Ham United','9937':'Brentford'})

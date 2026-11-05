import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
import dataset_builder
from collection import datasetIndexCache as indexing
from concordance import package_state as tracking
from collection.paths import collection_directory
from storage.pandasStore import atomic_json
from tests.test_providers import Sofa,Fot
from collection.seasonRunner import SeasonRunner


class IdentityProjectionTests(unittest.TestCase):
    def test_recipe_changes_only_for_identity_projection_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'providers').mkdir();(root/'collection').mkdir()
            builder=root/'dataset_builder.py'
            builder.write_text('def _source_id_rows():\n    return 1\ndef package_collection():\n    return 2\n')
            stats=root/'providers/statistics.py';stats.write_text('# stats v1')
            with patch.object(config,'PROJECT_ROOT',root):
                before=indexing.recipe()
                stats.write_text('# stats v2')
                builder.write_text('def _source_id_rows():\n    return 1\ndef package_collection():\n    return 3\n')
                self.assertEqual(indexing.recipe(),before)
                builder.write_text('def _source_id_rows():\n    return 4\ndef package_collection():\n    return 3\n')
                self.assertNotEqual(indexing.recipe(),before)

    def test_maps_and_event_files_do_not_invalidate_identity_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)
            atomic_json(source/'config.json',{'competition':'Premier League','season':'2024/2025'})
            atomic_json(source/'schedule.json',{'events':[]});before=indexing.source_signature(source)
            atomic_json(source/'raw/sofascore/map.json',{'payload':{'heatmap':[]}})
            atomic_json(source/'raw/whoscored_events/events.json',{'events':[]})
            self.assertEqual(indexing.source_signature(source),before)
            atomic_json(source/'raw/fotmob/details.json',{'provider':'fotmob'})
            self.assertNotEqual(indexing.source_signature(source),before)

    def test_snapshot_signature_is_frozen_before_derived_exports_are_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'live';source=Path(temporary)/'snapshot'
            for base in (root,source):
                atomic_json(base/'premier-league/2024-2025/config.json',{'competition':'Premier League','season':'2024/2025'})
            season=source/'premier-league/2024-2025'
            with tracking.snapshot_inputs(root,source):
                before=indexing.source_signature(season)
                (season/'player_match_stats.csv').write_text('match_id,player_id\n1,2\n')
                self.assertEqual(indexing.source_signature(season),before)
            self.assertNotEqual(indexing.source_signature(season),before)

    def test_one_season_change_reuses_other_ids_even_when_packaging_recipe_changes(self):
        from tests import test_package_modes as fixtures
        helper=fixtures.PackageModeTests();helper.setUp();self.addCleanup(helper.doCleanups)
        self.assertEqual(helper.build(),0)
        from tests.test_providers import event
        atomic_json(helper.sources[1]/'schedule.json',{'events':[event(201),event(202)]})
        with patch('concordance.package_state.recipe',return_value='packaging-only-version'),patch('dataset_builder._source_id_rows',wraps=dataset_builder._source_id_rows) as reads:
            self.assertEqual(helper.build(),0)
        self.assertEqual([call.args[0].parent.name for call in reads.call_args_list],['bundesliga'])
        atomic_json(helper.sources[0]/'raw/sofascore/heatmap.json',{'payload':{'heatmap':[]}})
        with patch('concordance.package_state.recipe',return_value='packaging-only-version'),patch('dataset_builder._source_id_rows',side_effect=AssertionError('ID cache invalidated by map')):
            self.assertEqual(helper.build(),0)

    def test_checkpoint_projection_does_not_rebuild_domain_or_action_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'premier-league/2024-2025'
            runner=SeasonRunner({'sofascore':Sofa(),'fotmob':Fot()},source,
                                providers=['sofascore','fotmob'],spatial=(),report=lambda _:None)
            runner.run('EPL','2024',limit=1,build_csvs=False)
            # Stale flat exports must not hide newer checkpoint identities.
            (source/'player_match_stats.csv').write_text('match_id,team,player_id,player_name\n')
            (source/'identities.csv').write_text('match_id,team,player_id,provider,provider_player_id\n')
            players,teams,matches={},{},{}
            with patch('storage.pandasStore.PandasStore.from_csv',side_effect=AssertionError('decoded full match graph')):
                dataset_builder._source_id_rows(source,players,teams,matches)
            self.assertEqual({r['player_name'] for r in players.values()},{'Player One','Player Two'})
            self.assertTrue(all(r['_ids']['sofascore_id'] for r in players.values()))
            self.assertTrue(all(r['_ids']['fotmob_id'] for r in players.values()))
            self.assertEqual(len(teams),2);self.assertEqual(len(matches),2)  # Full saved schedule retained.

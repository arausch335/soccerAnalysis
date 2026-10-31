import json
from pathlib import Path
import tempfile
import unittest

from fixtures.availability import annotate,stats_evidence


class AvailabilityTests(unittest.TestCase):
    def test_only_explicit_targets_without_saved_stats_are_pending(self):
        rows=[{'sofascore_match_id':str(mid),'competition':comp,'season':season,'status':status}
            for mid,comp,season,status in [(1,'Premier League','2024-2025','finished'),
                (2,'Premier League','2024-2025','finished'),(3,'Premier League','2010-2011','finished'),
                (4,'Champions League Qualification','2024-2025','notstarted'),
                (5,'Premier League','2024-2025','cancelled')]]
        annotate(rows,observed={('sofascore','1')},targets={('Premier League','2024-2025'),('Champions League Qualifiers','2024-2025')})
        self.assertEqual([r['has_player_stats'] for r in rows],[True,False,False,False,False])
        self.assertEqual([r['is_scheduled_for_scraping'] for r in rows],[False,True,False,True,False])

    def test_rejected_peer_cannot_imply_stats_on_the_baseline_fixture(self):
        rows=[{'sofascore_match_id':'1','fotmob_match_id':'2','fotmob_fixture_check':'mismatch'},
              {'sofascore_id':'1','fotmob_id':'2','safe_for_cross_provider_joins':'False'}]
        annotate(rows,observed={('fotmob','2')},targets=set())
        self.assertFalse(any(r['has_player_stats'] for r in rows))

    def test_inventory_requires_stats_not_an_id_bridge_or_event_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);folder=base/'ensemble_data/workers';folder.mkdir(parents=True)
            rows={'a':{'namespace':'sofascore','match_id':'1','resources':{'sofascore_stats':'complete'}},
                  'b':{'namespace':'sofascore','match_id':'2','resources':{'fotmob_fixture':'complete'}},
                  'c':{'namespace':'whoscored_events','match_id':'3','resources':{'whoscored_events':'complete'}},
                  'd':{'namespace':'fotmob_players','match_id':'4','resources':{'fotmob_stats':'complete'}}}
            (folder/'main.json').write_text(json.dumps({'records':rows}))
            self.assertEqual(stats_evidence(base/'data',base/'dataset'),{('sofascore','1'),('fotmob','4')})

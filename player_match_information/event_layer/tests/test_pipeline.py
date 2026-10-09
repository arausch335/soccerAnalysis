import json
import unittest

import pandas as pd

from event_layer.pipeline import digest_events


def row(index, event_id, team, player, event_type, outcome, second, x, y, end_x=None, end_y=None, qualifiers=()):
    return {
        "match_id": 1, "event_index": index, "provider_event_id": 1000 + index, "event_id": event_id,
        "period": 1, "period_name": "FirstHalf", "minute": 0, "second": second, "expanded_minute": 0,
        "team_id": team, "team_name": f"Team {team}", "player_id": player, "player_name": f"Player {player}",
        "event_type": event_type, "outcome": outcome, "x": x, "y": y, "end_x": end_x, "end_y": end_y,
        "is_touch": True, "qualifiers_json": json.dumps([
            {"type": {"displayName": name}, **({"value": value} if value is not None else {})}
            for name, value in qualifiers
        ]),
    }


class PipelineTests(unittest.TestCase):
    def test_enrichment_relationships_possessions_and_features(self):
        raw = pd.DataFrame([
            row(0, 1, 10, 101, "Pass", "Successful", 0, 20, 50, 50, 50),
            row(1, 2, 10, 102, "BallTouch", "Successful", 2, 50, 50),
            row(2, 3, 10, 102, "TakeOn", "Successful", 4, 55, 50, qualifiers=(("OppositeRelatedEvent", "4"),)),
            row(3, 4, 20, 201, "Challenge", "Unsuccessful", 4, 45, 50, qualifiers=(("OppositeRelatedEvent", "3"),)),
            row(4, 5, 10, 102, "OffsidePass", "Successful", 8, 60, 50, 80, 50),
            row(5, 6, 20, 202, "Pass", "Successful", 10, 30, 50, 45, 50),
        ])
        result = digest_events(raw)
        events = result.events_enriched.set_index("event_index")
        self.assertEqual(events.at[0, "inferred_receiver_id"], 102)
        self.assertEqual(events.at[0, "receiver_confidence"], "high")
        self.assertTrue(events.at[3, "is_paired_duplicate"])
        self.assertEqual(events.at[4, "effective_outcome"], "possession_lost_offside")
        self.assertNotEqual(events.at[4, "possession_id"], events.at[5, "possession_id"])
        player = result.player_match_features.query("player_id == 101").iloc[0]
        self.assertEqual(player["passes_attempted"], 1)
        self.assertEqual(player["passes_completed"], 1)

    def test_schema_rejects_missing_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required"):
            digest_events(pd.DataFrame({"match_id": [1]}))

    def test_duplicate_provider_ids_are_disambiguated(self):
        first = row(0, 1, 10, 101, "Pass", "Successful", 0, 20, 50, 30, 50)
        second = row(1, 2, 10, 102, "BallTouch", "Successful", 1, 30, 50)
        second["provider_event_id"] = first["provider_event_id"]
        events = digest_events(pd.DataFrame([first, second])).events_enriched
        self.assertFalse(events["event_uid"].duplicated().any())
        self.assertTrue(events["event_uid"].str.contains(":index:").all())


if __name__ == "__main__":
    unittest.main()

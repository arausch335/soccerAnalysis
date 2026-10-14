import unittest
from copy import deepcopy
from models import *
from providers import SofaScoreLoader


def event(i, timestamp=100):
    return {'id': i, 'startTimestamp': timestamp, 'season': {'id': 10},
            'homeTeam': {'id': 1, 'name': 'Home'}, 'awayTeam': {'id': 2, 'name': 'Away'},
            'status': {'type': 'finished'}}


class Client:
    def __init__(self): self.calls = []
    def lineups(self, match_id):
        self.calls.append(match_id)
        return {'confirmed': True, 'home': {'players': [
            {'player': {'id': 7, 'name': 'Forward', 'position': 'F'},
             'substitute': False, 'statistics': {'minutesPlayed': 90, 'totalPass': 10}},
            {'player': {'id': 8, 'name': 'Midfielder', 'position': 'M'},
             'substitute': True, 'statistics': {}}]}, 'away': {'players': []}}


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.loader = SofaScoreLoader(self.client)
        self.season = self.loader.select_season(5, 'League', 10, '2026')

    def test_sequential_shared_entities_and_match_scoping(self):
        iterator = self.loader.iter_matches(self.season, [event(2, 200), event(1), event(1)])
        first = next(iterator)
        self.assertEqual(self.client.calls, [1])
        second = next(iterator)
        self.assertIs(first.home_team, second.home_team)
        self.assertIsNot(first.home_lineup, second.home_lineup)
        self.assertIs(first.home_lineup.players[0].player, second.home_lineup.players[0].player)
        self.assertEqual(list(first.home_team.matches), [1, 2])
        self.assertEqual(len(first.home_lineup.attackers), 1)
        self.assertEqual(first.home_lineup.substitutes[0].role_group, PlayerRoleGroup.UNKNOWN)
        self.assertIsNone(first.home_lineup.substitutes[0].minutes_played)
        self.assertEqual(first.home_lineup.players[0].stats.raw['totalPass'], 10)
        list(self.loader.iter_matches(self.season, [event(1)]))
        self.assertEqual(self.client.calls, [1, 2])

    def test_traits_weight_counts_and_exclude_future_and_target(self):
        matches = list(self.loader.iter_matches(self.season, [event(1), event(2, 200), event(3, 300)]))
        player = self.loader.dataset.players[7]
        player.performances[1].stats.record_action('passing', 10, 5)
        player.performances[2].stats.record_action('passing', 90, 90)
        player.performances[3].stats.record_action('passing', 1, 0)
        snapshot = derive_traits(player, matches[2])
        self.assertEqual(snapshot.passing.values['completion_rate'], .95)
        self.assertEqual(snapshot.source_match_ids, [1, 2])
        self.assertEqual(derive_traits(player, matches[1]).source_match_ids, [1])
        self.assertEqual(derive_traits(player, matches[2], 1).source_match_ids, [2])

    def test_missing_and_zero(self):
        self.assertIsNone(AttemptCompletion(0, 0).rate)
        self.assertIsNone(AttemptCompletion(10, None).rate)
        self.assertEqual(AttemptCompletion(10, 0).rate, 0)
        with self.assertRaises(ValueError): AttemptCompletion(1, 2)
        raw = {'nested': {'value': 2}}
        stats = PlayerMatchStats.from_sofascore(raw)
        raw['nested']['value'] = 3
        self.assertEqual(stats.raw['nested']['value'], 2)

    def test_invalid_relationships(self):
        match = next(self.loader.iter_matches(self.season, [event(1)]))
        with self.assertRaises(ValueError): Lineup(match, match.home_team, MatchSide.AWAY)
        with self.assertRaises(ValueError): match.opponent_of(Team(99, 'Other'))
        with self.assertRaises(ValueError): match.home_lineup.add_player(match.home_lineup.players[0])
        bad = event(2); bad['season']['id'] = 11
        with self.assertRaises(ValueError): list(self.loader.iter_matches(self.season, [bad]))

    def test_request_failure_can_retry(self):
        original = self.client.lineups
        def fail(_): raise RuntimeError('offline')
        self.client.lineups = fail
        with self.assertRaises(RuntimeError): list(self.loader.iter_matches(self.season, [event(1)]))
        self.assertFalse(self.loader.dataset.matches)
        self.assertFalse(self.season.matches)
        self.client.lineups = original
        self.assertEqual(len(list(self.loader.iter_matches(self.season, [event(1)]))), 1)

if __name__ == '__main__': unittest.main()

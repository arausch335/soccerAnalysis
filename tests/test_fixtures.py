import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from fixtures import collector as collect


SEASON = {"id": 2746, "name": "Premier League 10/11", "year": "10/11"}
COMPETITION = collect.COMPETITIONS[0]


def event(identifier=1, kickoff="2010-01-01T00:00:00+00:00"):
    return dict(id=identifier, startTimestamp=int(datetime.fromisoformat(kickoff).timestamp()),
                homeTeam=dict(id=10, name="Home"), awayTeam=dict(id=20, name="Away"),
                tournament=dict(name="Premier League", uniqueTournament=dict(id=17)),
                season=dict(id=2746), status=dict(type="finished", code=100),
                homeScore=dict(current=2, period1=1), awayScore=dict(current=0, period1=0))


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = collect.Store(self.temp.name, "2010-01-01")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_cutoff_deduplication_and_export(self):
        client = FakeClient([
            dict(events=[event(1, "2009-12-31T23:59:59+00:00"), event(2)], hasNextPage=True),
            dict(events=[event(2), event(3)], hasNextPage=False),
            dict(events=[event(3)], hasNextPage=False),
        ])
        collect.collect_season(client, self.store, COMPETITION, SEASON)
        report = self.store.export("complete")
        self.assertEqual(report["fixtures"], 2)
        self.assertEqual(report["competitions"][0]["seasons"][0]["status"], "complete")
        raw = (self.store.folder / "fixtures.jsonl").read_text().splitlines()
        self.assertEqual({json.loads(row)["id"] for row in raw}, {2, 3})
        self.assertIn("2010-01-01T00:00:00+00:00", (self.store.folder / "fixtures.csv").read_text())

    def test_complete_season_resumes_without_requests(self):
        collect.collect_season(FakeClient([dict(events=[event()], hasNextPage=False),
                                          dict(events=[], hasNextPage=False)]),
                               self.store, COMPETITION, SEASON)
        client = FakeClient([])
        collect.collect_season(client, self.store, COMPETITION, SEASON)
        self.assertEqual(client.calls, [])

    def test_incomplete_page_cannot_be_reported_as_complete(self):
        client = FakeClient([dict(events=[event()], hasNextPage=True),
                             collect.CollectionError("HTTP 404")])
        with self.assertRaises(collect.CollectionError):
            collect.collect_season(client, self.store, COMPETITION, SEASON)
        report = self.store.export("incomplete")
        self.assertEqual(report["fixtures"], 1)
        self.assertEqual(report["competitions"][0]["seasons"][0]["status"], "incomplete")

    def test_no_progress_and_missing_pagination_fail(self):
        for responses in ([dict(events=[], hasNextPage=True)], [dict(events=[event()])]):
            with self.assertRaises(collect.CollectionError):
                collect.collect_season(FakeClient(responses), self.store, COMPETITION, SEASON)

    def test_wrong_competition_fails(self):
        source = event()
        source["tournament"]["uniqueTournament"]["id"] = 8
        with self.assertRaises(collect.CollectionError):
            collect.normalize(source, COMPETITION, SEASON)

    def test_blank_scores_are_not_zero(self):
        source = event()
        source.update(homeScore=None, awayScore={}, roundInfo=None)
        row = collect.normalize(source, COMPETITION, SEASON)
        self.assertIsNone(row["home_score"])
        self.assertIsNone(row["away_score"])

    def test_cached_response_makes_no_request(self):
        self.store.cache("/sample", {"events": []})
        with patch("urllib.request.urlopen") as opener:
            self.assertEqual(collect.Client(self.store).get("/sample"), {"events": []})
        opener.assert_not_called()

    def test_interrupted_run_replays_cache_and_fetches_only_missing_pages(self):
        class Response(io.BytesIO):
            status = 200

        def response(events, more=False):
            return Response(json.dumps(dict(events=events, hasNextPage=more)).encode())

        forbidden = urllib.error.HTTPError(collect.BASE, 403, "Forbidden", {}, io.BytesIO(b'{}'))
        client = collect.Client(self.store)
        with patch.object(client, "wait_until"), patch("urllib.request.urlopen", side_effect=[
                response([event(1)], True), forbidden, response([event(2)]), response([])]) as opener:
            with self.assertRaises(collect.Forbidden):
                collect.collect_season(client, self.store, COMPETITION, SEASON)
            collect.collect_season(client, self.store, COMPETITION, SEASON)
        self.assertEqual(opener.call_count, 4)
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertEqual(sum(url.endswith("/last/0") for url in urls), 1)
        self.assertEqual(self.store.export("complete")["fixtures"], 2)

    def test_403_stops_after_one_attempt_and_retains_pacing(self):
        error = urllib.error.HTTPError(collect.BASE + "/sample", 403, "Forbidden", {},
                                       io.BytesIO(b'{"error":{"reason":"challenge"}}'))
        with patch("urllib.request.urlopen", side_effect=error) as opener:
            with self.assertRaises(collect.Forbidden):
                collect.Client(self.store).get("/sample")
        self.assertEqual(opener.call_count, 1)
        self.assertGreater(self.store.get_meta("next_request_at"), 0)
        self.assertIsNone(self.store.cached("/sample"))

    def test_429_honors_retry_after_without_immediate_retry(self):
        error = urllib.error.HTTPError(collect.BASE + "/sample", 429, "Rate limited",
                                       {"Retry-After": "900"}, io.BytesIO(b'{}'))
        client = collect.Client(self.store, budget=1)
        with patch("urllib.request.urlopen", side_effect=error) as opener, \
                patch("time.time", return_value=1000), patch("random.uniform", return_value=0):
            with self.assertRaises(collect.BudgetReached):
                client.get("/sample")
        self.assertEqual(opener.call_count, 1)
        self.assertGreaterEqual(self.store.get_meta("next_request_at"), 1900)

    def test_retry_after_date(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
        self.assertEqual(collect.retry_after_seconds("Fri, 09 Oct 2026 00:15:00 GMT", now), 900)

    def test_years_and_conference_coverage(self):
        self.assertEqual(collect.season_start_year({"year": "09/10"}), 2009)
        self.assertEqual(collect.season_start_year({"year": "2010/2011"}), 2010)
        self.assertEqual(collect.season_start_year({"year": "99/00"}), 1999)
        with self.assertRaises(collect.CollectionError):
            collect.season_start_year({"year": "unknown"})
        report = self.store.export("blocked")
        conference = report["competitions"][-1]
        self.assertEqual(conference["undiscovered_season_start_years"][0], 2021)

    def test_checkpoint_rejects_different_cutoff(self):
        with self.assertRaises(collect.CollectionError):
            collect.Store(self.temp.name, "2011-01-01")


if __name__ == "__main__":
    unittest.main()

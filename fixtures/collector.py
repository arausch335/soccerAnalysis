#!/usr/bin/env python3
"""Serial, checkpointed collection of Sofascore football fixtures (stdlib only)."""
import argparse
import csv
import email.utils
import json
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .locking import output_lock

BASE = "https://www.sofascore.com/api/v1"
COMPETITIONS = [
    (17, "Premier League", "England", 1),
    (18, "Championship", "England", 2),
    (8, "LaLiga", "Spain", 1),
    (54, "LaLiga 2", "Spain", 2),
    (35, "Bundesliga", "Germany", 1),
    (44, "2. Bundesliga", "Germany", 2),
    (23, "Serie A", "Italy", 1),
    (53, "Serie B", "Italy", 2),
    (34, "Ligue 1", "France", 1),
    (182, "Ligue 2", "France", 2),
    (7, "UEFA Champions League", "Europe", None),
    (679, "UEFA Europa League", "Europe", None),
    (17015, "UEFA Conference League", "Europe", None),
]
FIELDS = ["event_id", "competition_id", "competition", "country", "division",
          "season_id", "season", "kickoff_utc", "kickoff_timestamp", "home_id",
          "home_team", "away_id", "away_team", "status", "status_code",
          "home_score", "away_score", "home_halftime", "away_halftime",
          "home_normaltime", "away_normaltime", "home_penalties", "away_penalties",
          "round", "round_name", "stage", "event_url"]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class CollectionError(Exception):
    pass


class Forbidden(CollectionError):
    pass


class BudgetReached(CollectionError):
    pass


class SourceUnavailable(CollectionError):
    pass


def season_start_year(season):
    value = str(season.get("year", ""))
    found = re.fullmatch(r"(\d{2}|\d{4})(?:[/\-](\d{2}|\d{4}))?", value)
    if not found:
        raise CollectionError(f"Unrecognized season year: {value!r}")
    year = int(found.group(1))
    if len(found.group(1)) == 2:
        year += 1900 if year >= 70 else 2000
    return year


def retry_after_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, AttributeError, OverflowError):
            return 0


class Store:
    def __init__(self, folder, start_date):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / "fixtures.sqlite")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS responses (
                path TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seasons (
                competition_id INTEGER, season_id INTEGER, name TEXT, year TEXT,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                PRIMARY KEY (competition_id, season_id));
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY, competition_id INTEGER NOT NULL,
                season_id INTEGER NOT NULL, kickoff_timestamp INTEGER NOT NULL,
                normalized TEXT NOT NULL, raw TEXT NOT NULL);
        """)
        previous = self.get_meta("start_date")
        if previous and previous != start_date:
            self.db.close()
            raise CollectionError("Start date differs from checkpoint; use another output directory")
        self.set_meta("start_date", start_date)
        self.start_timestamp = int(datetime.fromisoformat(start_date).replace(
            tzinfo=timezone.utc).timestamp())

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def cached(self, path):
        row = self.db.execute("SELECT payload FROM responses WHERE path=?", (path,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, path, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                            (path, json.dumps(payload), utcnow()))

    def export(self, status=None, error=None):
        if status is not None:
            self.set_meta("run_status", status)
            self.set_meta("run_error", error)
        rows = list(self.db.execute("SELECT normalized,raw FROM events ORDER BY kickoff_timestamp,event_id"))
        with (self.folder / "fixtures.csv.tmp").open("w", newline="", encoding="utf-8") as csv_file, \
                (self.folder / "fixtures.jsonl.tmp").open("w", encoding="utf-8") as raw_file:
            writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
            writer.writeheader()
            for normalized, raw in rows:
                writer.writerow(json.loads(normalized))
                raw_file.write(raw + "\n")
        (self.folder / "fixtures.csv.tmp").replace(self.folder / "fixtures.csv")
        (self.folder / "fixtures.jsonl.tmp").replace(self.folder / "fixtures.jsonl")
        coverage = []
        for cid, name, country, division in COMPETITIONS:
            seasons = []
            for sid, season_name, year, season_status, season_error in self.db.execute(
                    "SELECT season_id,name,year,status,error FROM seasons WHERE competition_id=? ORDER BY year",
                    (cid,)):
                count, first, last = self.db.execute(
                    "SELECT COUNT(*),MIN(kickoff_timestamp),MAX(kickoff_timestamp) FROM events WHERE competition_id=? AND season_id=?",
                    (cid, sid)).fetchone()
                seasons.append(dict(id=sid, name=season_name, year=year, status=season_status,
                                    error=season_error, fixtures=count, first_timestamp=first, last_timestamp=last))
            first_year = max(date.fromisoformat(self.get_meta("start_date")).year - 1,
                             2021 if cid == 17015 else 0)
            discovered_years = {season_start_year(s) for s in seasons}
            missing_years = sorted(set(range(first_year, date.today().year + 1)) - discovered_years)
            coverage.append(dict(id=cid, name=name, country=country, division=division,
                                 season_discovery=("complete" if self.cached(f"/unique-tournament/{cid}/seasons") is not None
                                                   else "not_completed"), seasons=seasons,
                                 discovery_error=self.get_meta(f"discovery_error_{cid}"),
                                 undiscovered_season_start_years=missing_years))
        report = dict(updated_at=utcnow(), source=BASE, start_date=self.get_meta("start_date"),
                      run_status=self.get_meta("run_status"), error=self.get_meta("run_error"),
                      fixtures=len(rows), competitions=coverage,
                      completeness="API pagination completed means pages traversed; historical source coverage is not independently verified.",
                      includes="Published fixtures dated from start_date, including future matches in discovered seasons.")
        (self.folder / "coverage.json.tmp").write_text(json.dumps(report, indent=2) + "\n")
        (self.folder / "coverage.json.tmp").replace(self.folder / "coverage.json")
        return report


class Client:
    def __init__(self, store, minimum=15, maximum=25, budget=None, attempts=5):
        self.store = store
        self.minimum, self.maximum = minimum, maximum
        self.budget, self.attempts = budget, attempts
        self.requests = 0

    def log(self, **item):
        with (self.store.folder / "requests.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(time=utcnow(), **item)) + "\n")

    def wait_until(self, deadline):
        while deadline > time.time():
            time.sleep(min(30, deadline - time.time()))

    def get(self, path):
        cached = self.store.cached(path)
        if cached is not None:
            return cached
        for attempt in range(self.attempts):
            if self.budget is not None and self.requests >= self.budget:
                raise BudgetReached("Request budget reached; checkpoints saved")
            self.wait_until(self.store.get_meta("next_request_at") or 0)
            self.requests += 1
            request = urllib.request.Request(BASE + path, headers={
                "Accept": "application/json", "User-Agent": "SofascoreFixtureCollector/1.0"})
            status, retry_header, problem = None, None, None
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    status = response.status
                    payload = json.load(response)
                if not isinstance(payload, dict) or "error" in payload:
                    raise CollectionError(f"Unexpected JSON response: {path}")
                self.log(path=path, status=status, attempt=attempt + 1)
                self.store.cache(path, payload)
                return payload
            except urllib.error.HTTPError as exc:
                status, retry_header = exc.code, exc.headers.get("Retry-After")
                body = exc.read(4096).decode("utf-8", errors="replace")
                self.log(path=path, status=status, attempt=attempt + 1, response=body)
                if status == 403:
                    raise Forbidden(f"HTTP 403 at {path}; stopped without retry") from exc
                if status == 401:
                    raise CollectionError(f"HTTP 401 at {path}; stopped without retry") from exc
                if status in (404, 410):
                    raise SourceUnavailable(f"HTTP {status} at {path}; source unavailable") from exc
                if status != 429 and not 500 <= status <= 599:
                    raise CollectionError(f"HTTP {status} at {path}; season remains incomplete") from exc
                problem = f"HTTP {status}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
                self.log(path=path, status=status, attempt=attempt + 1, error=problem)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.log(path=path, status=status, attempt=attempt + 1, error="Non-JSON response")
                raise CollectionError(f"Non-JSON response at {path}; stopped") from exc
            finally:
                self.store.set_meta("next_request_at", time.time() + random.uniform(self.minimum, self.maximum))
            delay = max(60 * 2 ** attempt, retry_after_seconds(retry_header)) + random.uniform(0, 10)
            self.store.set_meta("next_request_at", time.time() + delay)
            if attempt + 1 == self.attempts:
                raise CollectionError(f"Retries exhausted at {path}: {problem}")
            print(f"{problem}; waiting at least {delay:.0f}s", flush=True)
        raise AssertionError("Unreachable")


def normalize(event, competition, season):
    cid, name, country, division = competition
    if not isinstance(event, dict) or not isinstance(event.get("id"), int) or not isinstance(event.get("startTimestamp"), int):
        raise CollectionError("Event missing numeric id or kickoff timestamp")
    tournament = event.get("tournament") or {}
    source_cid = (tournament.get("uniqueTournament") or {}).get("id")
    if source_cid is not None and source_cid != cid:
        raise CollectionError(f"Unexpected competition {source_cid} in requested competition {cid}")
    source_sid = (event.get("season") or {}).get("id")
    if source_sid is not None and source_sid != season["id"]:
        raise CollectionError(f"Unexpected season {source_sid} in requested season {season['id']}")
    home, away = event.get("homeTeam") or {}, event.get("awayTeam") or {}
    if not home.get("name") or not away.get("name"):
        raise CollectionError("Event missing home or away team name")
    home_score, away_score = event.get("homeScore") or {}, event.get("awayScore") or {}
    status, round_info = event.get("status") or {}, event.get("roundInfo") or {}
    row = dict(event_id=event["id"], competition_id=cid, competition=name,
               country=country, division=division, season_id=season["id"], season=season["year"],
               kickoff_utc=datetime.fromtimestamp(event["startTimestamp"], timezone.utc).isoformat(),
               kickoff_timestamp=event["startTimestamp"], home_id=home.get("id"),
               home_team=home["name"], away_id=away.get("id"), away_team=away["name"],
               status=status.get("type"), status_code=status.get("code"),
               round=round_info.get("round"), round_name=round_info.get("name"), stage=tournament.get("name"),
               event_url=(f"https://www.sofascore.com/football/match/{event['slug']}/{event['customId']}#id:{event['id']}"
                          if event.get("slug") and event.get("customId") else None))
    for suffix, key in [("score", "current"), ("halftime", "period1"),
                        ("normaltime", "normaltime"), ("penalties", "penalties")]:
        row[f"home_{suffix}"], row[f"away_{suffix}"] = home_score.get(key), away_score.get(key)
    return row


def collect_season(client, store, competition, season):
    cid = competition[0]
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO seasons (competition_id,season_id,name,year) VALUES (?,?,?,?)",
                         (cid, season["id"], season.get("name"), season["year"]))
        state = store.db.execute("SELECT status FROM seasons WHERE competition_id=? AND season_id=?",
                                 (cid, season["id"])).fetchone()[0]
    if state == "complete":
        return
    with store.db:
        store.db.execute("UPDATE seasons SET status='running',error=NULL WHERE competition_id=? AND season_id=?",
                         (cid, season["id"]))
    try:
        for direction in ("last", "next"):
            seen = set()
            for page in range(1000):
                path = f"/unique-tournament/{cid}/season/{season['id']}/events/{direction}/{page}"
                payload = client.get(path)
                events = payload.get("events")
                more = payload.get("hasNextPage")
                if not isinstance(events, list) or not isinstance(more, bool):
                    raise CollectionError(f"Invalid pagination response at {path}")
                normalized = [normalize(e, competition, season) for e in events]
                ids = {row["event_id"] for row in normalized}
                if more and (not ids or ids <= seen):
                    raise CollectionError(f"Pagination made no progress at {path}")
                seen.update(ids)
                with store.db:
                    for row, event in zip(normalized, events):
                        if row["kickoff_timestamp"] >= store.start_timestamp:
                            store.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                                             (row["event_id"], cid, season["id"], row["kickoff_timestamp"],
                                              json.dumps(row), json.dumps(event)))
                print(f"{competition[1]} {season['year']} {direction}/{page}: {len(events)} events", flush=True)
                if not more:
                    break
            else:
                raise CollectionError(f"Pagination limit reached for {competition[1]} {season['year']}")
        with store.db:
            store.db.execute("UPDATE seasons SET status='complete',error=NULL WHERE competition_id=? AND season_id=?",
                             (cid, season["id"]))
    except BaseException as exc:
        state = "blocked" if isinstance(exc, Forbidden) else "paused" if isinstance(exc, (BudgetReached, KeyboardInterrupt)) else "incomplete"
        with store.db:
            store.db.execute("UPDATE seasons SET status=?,error=? WHERE competition_id=? AND season_id=?",
                             (state, str(exc) or type(exc).__name__, cid, season["id"]))
        raise


def collect(client, store, competitions=COMPETITIONS):
    earliest = date.fromisoformat(store.get_meta("start_date")).year - 1
    latest = date.today().year
    for competition in competitions:
        try:
            payload = client.get(f"/unique-tournament/{competition[0]}/seasons")
        except SourceUnavailable as exc:
            store.set_meta(f"discovery_error_{competition[0]}", str(exc))
            print(f"Unavailable competition: {competition[1]}: {exc}", flush=True)
            continue
        seasons = payload.get("seasons")
        if not isinstance(seasons, list):
            raise CollectionError(f"Missing seasons for {competition[1]}")
        selected = []
        for season in seasons:
            if not isinstance(season, dict) or not isinstance(season.get("id"), int):
                raise CollectionError(f"Invalid season for {competition[1]}")
            if earliest <= season_start_year(season) <= latest:
                selected.append(season)
        if not selected:
            raise CollectionError(f"No in-range seasons discovered for {competition[1]}")
        for season in sorted(selected, key=season_start_year):
            with store.db:
                store.db.execute("INSERT OR IGNORE INTO seasons (competition_id,season_id,name,year) VALUES (?,?,?,?)",
                                 (competition[0], season["id"], season.get("name"), season["year"]))
        store.export("running")
        for season in sorted(selected, key=season_start_year):
            try:
                collect_season(client, store, competition, season)
            except SourceUnavailable as exc:
                print(f"Unavailable season: {competition[1]} {season['year']}: {exc}", flush=True)
            store.export("running")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/fixtures"))
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--min-delay", type=float, default=15)
    parser.add_argument("--max-delay", type=float, default=25)
    parser.add_argument("--max-requests", type=int, help="Pause after this many network requests")
    parser.add_argument("--competition", type=int, action="append", help="Collect selected competition IDs only")
    parser.add_argument("--export-only", action="store_true", help="Export checkpoints without any network requests")
    args = parser.parse_args(argv)
    try:
        date.fromisoformat(args.start_date)
    except ValueError:
        parser.error("--start-date must use YYYY-MM-DD")
    if args.min_delay < 15 or args.max_delay < args.min_delay:
        parser.error("minimum delay must be at least 15 seconds; maximum must be >= minimum")
    if args.max_requests is not None and args.max_requests < 1:
        parser.error("--max-requests must be positive")
    competitions = [c for c in COMPETITIONS if args.competition is None or c[0] in args.competition]
    if args.competition and set(args.competition) - {c[0] for c in COMPETITIONS}:
        parser.error("unknown competition ID")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        lock = output_lock(args.output)
        lock.__enter__()
    except BlockingIOError:
        parser.exit(2, "Another collector is using this output directory\n")
    try:
        store = Store(args.output, args.start_date)
        try:
            if args.export_only:
                report = store.export()
            else:
                try:
                    collect(Client(store, args.min_delay, args.max_delay, args.max_requests), store, competitions)
                    report = store.export("complete" if len(competitions) == len(COMPETITIONS) else "subset_complete")
                    requested = {c[0] for c in competitions}
                    if any(c["undiscovered_season_start_years"] or
                           any(s["status"] != "complete" for s in c["seasons"])
                           for c in report["competitions"] if c["id"] in requested):
                        report = store.export("completed_with_gaps")
                except (CollectionError, KeyboardInterrupt) as exc:
                    status = "blocked" if isinstance(exc, Forbidden) else "paused" if isinstance(exc, (BudgetReached, KeyboardInterrupt)) else "incomplete"
                    report = store.export(status, str(exc) or "Interrupted")
                    print(f"{status}: {report['error']} ({report['fixtures']} saved fixtures)", file=sys.stderr)
                    return 3 if status == "blocked" else 2
            print(f"{report['run_status']}: {report['fixtures']} fixtures; {args.output / 'coverage.json'}")
            return 2 if report["run_status"] in ("completed_with_gaps", "incomplete", "paused") else 3 if report["run_status"] == "blocked" else 0
        finally:
            store.db.close()
    finally:
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())

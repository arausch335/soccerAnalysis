"""Resumable, sequential collection of a team's earliest finished season matches."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from providers import SofaScoreError, SofaScoreRequestError, SofaScoreLoader
from storage import PandasStore
from storage.pandasStore import atomic_json, atomic_replace


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='') as stream:
            frame.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class CollectionConfig:
    competition_id: int = 17
    competition_name: str = 'Premier League'
    season_id: int = 61627
    season_name: str = 'Premier League 24/25'
    team_id: int = 44
    team_name: str = 'Liverpool'
    limit: int = 10

    def __post_init__(self):
        if self.limit < 1:
            raise ValueError('limit must be positive')


class CachedResponses:
    """Raw responses in a pandas CSV, saved before domain conversion."""
    columns = ['endpoint', 'fetched_at', 'payload']

    def __init__(self, client, path):
        self.client, self.path = client, Path(path)
        self.frame = (pd.read_csv(path, keep_default_na=False) if self.path.exists()
                      else pd.DataFrame(columns=self.columns))

    def fetch(self, endpoint, request):
        found = self.frame[self.frame['endpoint'] == endpoint]
        if not found.empty:
            return json.loads(found.iloc[-1]['payload'])
        payload = request()
        row = {'endpoint': endpoint, 'fetched_at': now(),
               'payload': json.dumps(payload, ensure_ascii=False, allow_nan=False)}
        updated = pd.concat([self.frame, pd.DataFrame([row])], ignore_index=True)
        atomic_csv(updated, self.path)
        self.frame = updated
        return payload

    def lineups(self, match_id):
        return self.fetch(f'event/{match_id}/lineups', lambda: self.client.lineups(match_id))

    def invalidate_lineups(self, match_id):
        self.frame = self.frame[self.frame['endpoint'] != f'event/{match_id}/lineups']
        atomic_csv(self.frame, self.path)


class CollectionRunner:
    def __init__(self, client, directory, config=None, report=print):
        self.client = client
        self.directory = Path(directory)
        self.config = config or CollectionConfig()
        self.report = report
        self.store = PandasStore(self.directory / 'dataset')
        self.raw = CachedResponses(client, self.directory / 'responses.csv')

    def _select(self, events):
        c = self.config
        candidates = {}
        for event in events:
            if event.get('season', {}).get('id') != c.season_id:
                continue
            if event.get('tournament', {}).get('uniqueTournament', {}).get('id') != c.competition_id:
                continue
            if event.get('status', {}).get('type') != 'finished':
                continue
            if c.team_id not in (event['homeTeam']['id'], event['awayTeam']['id']):
                continue
            candidates[event['id']] = event
        selected = sorted(candidates.values(), key=lambda e: (e['startTimestamp'], e['id']))[:c.limit]
        if len(selected) != c.limit:
            raise ValueError(f'Expected {c.limit} finished matches, found {len(selected)}')
        return selected

    def _schedule(self, events=None):
        config_path = self.directory / 'config.json'
        config = asdict(self.config)
        if config_path.exists():
            if json.loads(config_path.read_text(encoding='utf-8')) != config:
                raise ValueError('Output folder belongs to a different collection; use another folder')
        else:
            atomic_json(config_path, config)
        schedule_path = self.directory / 'schedule.csv'
        if schedule_path.exists():
            saved = pd.read_csv(schedule_path, keep_default_na=False)
            return self._select(saved['event'].map(json.loads))
        if events is None:
            c = self.config
            self.report(f'Finding {c.team_name} matches in {c.season_name}...')
            competition = self.raw.fetch(f'unique-tournament/{c.competition_id}',
                                        lambda: self.client.competition(c.competition_id))
            seasons = self.raw.fetch(f'unique-tournament/{c.competition_id}/seasons',
                                    lambda: self.client.seasons(c.competition_id))
            team = self.raw.fetch(f'team/{c.team_id}', lambda: self.client.team(c.team_id))
            if (competition['uniqueTournament']['id'] != c.competition_id
                    or c.season_id not in {s['id'] for s in seasons['seasons']}
                    or team['team']['id'] != c.team_id):
                raise ValueError('Provider selection does not match the collection configuration')
            events = self.client.iter_season_events(c.competition_id, c.season_id)
        selected = self._select(events)
        frame = pd.DataFrame([{'match_id': e['id'], 'kickoff': datetime.fromtimestamp(e['startTimestamp'], timezone.utc).isoformat(),
                               'home': e['homeTeam']['name'], 'away': e['awayTeam']['name'],
                               'event': json.dumps(e, ensure_ascii=False)} for e in selected])
        atomic_csv(frame, schedule_path)
        return selected

    def run(self, events=None):
        """Save per match; resume successes, retry failures, retain raw responses.

        Completion here means both lineups and their available embedded stats.
        It does not claim coverage of every optional provider endpoint.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        selected = self._schedule(events)
        loader = SofaScoreLoader(self.raw, self.store.from_csv() if self.store.exists else None)
        c = self.config
        season = loader.select_season(c.competition_id, c.competition_name, c.season_id, c.season_name)
        status_path = self.directory / 'collection.csv'
        previous = pd.read_csv(status_path, keep_default_na=False).to_dict('records') if status_path.exists() else []
        statuses = {int(r['match_id']): r for r in previous}

        for index, event in enumerate(selected, 1):
            match_id = event['id']
            old = statuses.get(match_id, {})
            if match_id in loader.dataset.matches:
                statuses[match_id] = {'match_id': match_id, 'status': 'complete',
                    'attempts': old.get('attempts', 0), 'updated_at': old.get('updated_at', now()), 'error': ''}
                self.report(f'[{index}/{len(selected)}] Already saved: {event["homeTeam"]["name"]} vs {event["awayTeam"]["name"]}')
                continue
            attempts = int(old.get('attempts', 0)) + 1
            status, error, stop = 'complete', '', False
            try:
                payload = self.raw.lineups(match_id)
                if not all(payload.get(side, {}).get('players') for side in ('home', 'away')):
                    self.raw.invalidate_lineups(match_id)
                    raise ValueError('Both populated lineups are required; response is incomplete')
                match = next(loader.iter_matches(season, [event]))
                match.metadata['collection'] = {'collected_at': now(), 'coverage': 'lineups_with_embedded_player_stats'}
                self.store.to_csv(loader.dataset)
            except (SofaScoreError, ValueError, KeyError, TypeError) as exc:
                status = 'unavailable' if isinstance(exc, SofaScoreRequestError) and exc.status_code == 404 else 'failed'
                error = str(exc)
                stop = isinstance(exc, SofaScoreRequestError) and exc.status_code in (403, 429)
                # Discard any partially built graph; the last committed dataset is authoritative.
                loader = SofaScoreLoader(self.raw, self.store.from_csv() if self.store.exists else None)
                season = loader.select_season(c.competition_id, c.competition_name, c.season_id, c.season_name)
            statuses[match_id] = {'match_id': match_id, 'status': status, 'attempts': attempts,
                                  'updated_at': now(), 'error': error}
            atomic_csv(pd.DataFrame(statuses.values()), status_path)
            self.report(f'[{index}/{len(selected)}] {event["homeTeam"]["name"]} vs {event["awayTeam"]["name"]}: {status}' + (f' ({error})' if error else ''))
            if stop:
                break
        # Also persist statuses rebuilt from an existing dataset after an interruption.
        atomic_csv(pd.DataFrame(statuses.values()), status_path)
        self.dataset = loader.dataset
        atomic_csv(self.store.player_stats_frame(self.dataset), self.directory / 'player_match_stats.csv')
        (self.directory / 'player_stats.csv').unlink(missing_ok=True)
        return pd.DataFrame([statuses.get(e['id'], {'match_id': e['id'], 'status': 'pending', 'attempts': 0,
                                                  'updated_at': '', 'error': ''}) for e in selected])

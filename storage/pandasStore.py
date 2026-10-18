"""Relational pandas tables and CSV checkpoints for the domain object graph."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd

from models import (FootballDataset, Competition, Season, Team, Player, Match,
                    Lineup, PlayerMatch, PlayerMatchStats, AttemptCompletion,
                    MatchStatus, MatchSide, PlayerRoleGroup, PlayerTraitSnapshot,
                    TraitCategory)

# JSON cells preserve nested provider values and nullable strings without CSV
# type inference turning names such as "NA" into missing values.
SCHEMA = {
    'competitions': {'id': 'Int64', 'name': 'json'},
    'seasons': {'competition_id': 'Int64', 'id': 'Int64', 'name': 'json'},
    'teams': {'id': 'Int64', 'name': 'json'},
    'players': {'id': 'Int64', 'name': 'json', 'position': 'json', 'information': 'json'},
    'team_players': {'team_id': 'Int64', 'player_id': 'Int64'},
    'team_competitions': {'team_id': 'Int64', 'competition_id': 'Int64'},
    'matches': {'id': 'Int64', 'competition_id': 'Int64', 'season_id': 'Int64',
                'kickoff': 'json', 'home_team_id': 'Int64', 'away_team_id': 'Int64',
                'home_score': 'Int64', 'away_score': 'Int64', 'round_number': 'Int64',
                'status': 'json', 'metadata': 'json'},
    'lineups': {'match_id': 'Int64', 'team_id': 'Int64', 'side': 'json',
               'formation': 'json', 'confirmed': 'boolean'},
    'performances': {'match_id': 'Int64', 'team_id': 'Int64', 'player_id': 'Int64',
                     'starter': 'boolean', 'position': 'json', 'shirt_number': 'Int64',
                     'minutes_played': 'Int64', 'role_group': 'json', 'raw_stats': 'json'},
    'actions': {'match_id': 'Int64', 'player_id': 'Int64', 'name': 'json',
                'attempts': 'Int64', 'completions': 'Int64'},
    'traits': {'player_id': 'Int64', 'before_match_id': 'Int64', 'matches_used': 'Int64',
               'method': 'json', 'window_size': 'Int64', 'source_match_ids': 'json',
               'categories': 'json'},
}
KEYS = {'competitions': ['id'], 'seasons': ['competition_id', 'id'], 'teams': ['id'],
        'players': ['id'], 'team_players': ['team_id', 'player_id'],
        'team_competitions': ['team_id', 'competition_id'], 'matches': ['id'],
        'lineups': ['match_id', 'team_id'], 'performances': ['match_id', 'player_id'],
        'actions': ['match_id', 'player_id', 'name'], 'traits': ['player_id', 'before_match_id']}
CATEGORIES = ('pace', 'physicality', 'passing', 'dribbling', 'shooting', 'defense')


def atomic_json(path, value, *, indent=None, sort_keys=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=indent, sort_keys=sort_keys)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean(value):
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


class PandasStore:
    """One writer per directory. References use IDs, not recursive serialization."""
    def __init__(self, directory):
        self.directory = Path(directory)

    @property
    def exists(self):
        return (self.directory / 'CURRENT.json').exists()

    @staticmethod
    def to_frames(dataset: FootballDataset) -> dict[str, pd.DataFrame]:
        rows = {name: [] for name in SCHEMA}
        for competition in dataset.competitions.values():
            rows['competitions'].append({'id': competition.id, 'name': competition.name})
            for season in competition.seasons.values():
                rows['seasons'].append({'competition_id': competition.id, 'id': season.id, 'name': season.name})
        for team in dataset.teams.values():
            rows['teams'].append({'id': team.id, 'name': team.name})
            rows['team_players'].extend({'team_id': team.id, 'player_id': p.id} for p in team.players.values())
            rows['team_competitions'].extend({'team_id': team.id, 'competition_id': c.id} for c in team.competitions.values())
        for player in dataset.players.values():
            rows['players'].append({'id': player.id, 'name': player.name, 'position': player.position,
                                    'information': deepcopy(player.information)})
            for trait in player.traits.values():
                rows['traits'].append({'player_id': player.id, 'before_match_id': trait.before_match.id,
                    'matches_used': trait.matches_used, 'method': trait.method, 'window_size': trait.window_size,
                    'source_match_ids': list(trait.source_match_ids),
                    'categories': {name: asdict(getattr(trait, name)) for name in CATEGORIES}})
        for match in dataset.matches.values():
            rows['matches'].append({'id': match.id, 'competition_id': match.season.competition.id,
                'season_id': match.season.id, 'kickoff': match.kickoff.isoformat(),
                'home_team_id': match.home_team.id, 'away_team_id': match.away_team.id,
                'home_score': match.home_score, 'away_score': match.away_score, 'round_number': match.round_number,
                'status': match.status.value, 'metadata': deepcopy(match.metadata)})
            for lineup in match.lineups:
                if lineup is None:
                    continue
                rows['lineups'].append({'match_id': match.id, 'team_id': lineup.team.id,
                    'side': lineup.side.value, 'formation': lineup.formation, 'confirmed': lineup.confirmed})
                for p in lineup.players:
                    rows['performances'].append({'match_id': match.id, 'team_id': p.team.id, 'player_id': p.player.id,
                        'starter': p.starter, 'position': p.position, 'shirt_number': p.shirt_number,
                        'minutes_played': p.minutes_played, 'role_group': p.role_group.value,
                        'raw_stats': deepcopy(p.stats.raw)})
                    rows['actions'].extend({'match_id': match.id, 'player_id': p.player.id,
                        'name': name, 'attempts': counts.attempts, 'completions': counts.completions}
                        for name, counts in p.stats.actions.items())
        frames = {}
        for name, schema in SCHEMA.items():
            frame = pd.DataFrame(rows[name], columns=list(schema))
            for column, dtype in schema.items():
                if dtype != 'json':
                    frame[column] = frame[column].astype(dtype)
            frames[name] = frame
        return frames

    @staticmethod
    def from_frames(frames: dict[str, pd.DataFrame]) -> FootballDataset:
        for name, schema in SCHEMA.items():
            if name not in frames or set(frames[name].columns) != set(schema):
                raise ValueError(f'Missing or unexpected columns in {name}')
            if frames[name].duplicated(KEYS[name]).any():
                raise ValueError(f'Duplicate keys in {name}')
        def records(name):
            return [{k: clean(v) for k, v in row.items()} for row in frames[name].to_dict('records')]
        dataset = FootballDataset()
        try:
            for r in records('competitions'):
                dataset.add_competition(Competition(**r))
            seasons = {}
            for r in records('seasons'):
                c = dataset.competitions[r.pop('competition_id')]
                season = Season(competition=c, **r)
                seasons[c.id, season.id] = season
            for r in records('teams'):
                dataset.add_team(Team(**r))
            for r in records('players'):
                dataset.add_player(Player(**deepcopy(r)))
            for r in records('matches'):
                season = seasons[r.pop('competition_id'), r.pop('season_id')]
                home = dataset.teams[r.pop('home_team_id')]
                away = dataset.teams[r.pop('away_team_id')]
                r['kickoff'] = datetime.fromisoformat(r['kickoff'])
                r['status'] = MatchStatus(r['status'])
                dataset.add_match(Match(season=season, home_team=home, away_team=away, **deepcopy(r)))
            for r in records('lineups'):
                match = dataset.matches[r.pop('match_id')]
                team = dataset.teams[r.pop('team_id')]
                r['side'] = MatchSide(r['side'])
                Lineup(match=match, team=team, **r)
            for r in records('performances'):
                match = dataset.matches[r.pop('match_id')]
                team = dataset.teams[r.pop('team_id')]
                player = dataset.players[r.pop('player_id')]
                lineup = match.lineup_for(team)
                if lineup is None:
                    raise ValueError('Performance references a missing lineup')
                stats = PlayerMatchStats.from_sofascore(r.pop('raw_stats'))
                r['role_group'] = PlayerRoleGroup(r['role_group'])
                lineup.add_player(PlayerMatch(player, match, team, lineup, stats=stats, **r))
            for r in records('actions'):
                p = dataset.players[r['player_id']].performances[r['match_id']]
                p.stats.actions[r['name']] = AttemptCompletion(r['attempts'], r['completions'])
            for r in records('traits'):
                player = dataset.players[r.pop('player_id')]
                target = dataset.matches[r.pop('before_match_id')]
                categories = r.pop('categories')
                trait = PlayerTraitSnapshot(player, target, **deepcopy(r))
                for name in CATEGORIES:
                    setattr(trait, name, TraitCategory(**deepcopy(categories[name])))
                player.traits[target.id] = trait
            for r in records('team_players'):
                dataset.teams[r['team_id']].players[r['player_id']] = dataset.players[r['player_id']]
            for r in records('team_competitions'):
                dataset.teams[r['team_id']].competitions[r['competition_id']] = dataset.competitions[r['competition_id']]
        except KeyError as exc:
            raise ValueError(f'Broken table reference: {exc}') from exc
        return dataset

    def to_csv(self, dataset: FootballDataset) -> Path:
        """Commit all CSV tables together; an interrupted write preserves CURRENT."""
        frames = self.to_frames(dataset)
        # Reject broken relationships before publishing an unreadable checkpoint.
        self.from_frames(frames)
        generation = uuid4().hex
        folder = self.directory / 'snapshots' / generation
        folder.mkdir(parents=True)
        for name, frame in frames.items():
            encoded = frame.copy()
            for column, dtype in SCHEMA[name].items():
                if dtype == 'json':
                    encoded[column] = encoded[column].map(lambda v: json.dumps(clean(v), ensure_ascii=False, allow_nan=False))
            with (folder / f'{name}.csv').open('w', encoding='utf-8', newline='') as stream:
                encoded.to_csv(stream, index=False)
                stream.flush()
                os.fsync(stream.fileno())
        atomic_json(self.directory / 'CURRENT.json', {'schema_version': 1, 'generation': generation})
        snapshots=self.directory/'snapshots'
        for candidate in snapshots.iterdir():
            name=candidate.name
            if (candidate.is_dir() and name!=generation and len(name)==32
                    and all(c in '0123456789abcdef' for c in name)):
                shutil.rmtree(candidate)
        return folder

    @property
    def current_directory(self):
        manifest = json.loads((self.directory / 'CURRENT.json').read_text(encoding='utf-8'))
        if manifest.get('schema_version') != 1:
            raise ValueError('Unsupported CSV schema version')
        generation = manifest['generation']
        if not isinstance(generation, str) or len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
            raise ValueError('Invalid checkpoint generation')
        return self.directory / 'snapshots' / generation

    def read_frames(self) -> dict[str, pd.DataFrame]:
        frames = {}
        folder = self.current_directory
        for name, schema in SCHEMA.items():
            frame = pd.read_csv(folder / f'{name}.csv', dtype=str, keep_default_na=False)
            if set(frame.columns) != set(schema):
                raise ValueError(f'Missing or unexpected columns in {name}')
            for column, dtype in schema.items():
                if dtype == 'json':
                    frame[column] = frame[column].map(json.loads)
                elif dtype == 'boolean':
                    if not frame[column].isin(['True', 'False', '']).all():
                        raise ValueError(f'Invalid boolean in {name}.{column}')
                    frame[column] = frame[column].map({'True': True, 'False': False, '': pd.NA}).astype('boolean')
                else:
                    frame[column] = frame[column].replace('', pd.NA).astype(dtype)
            frames[name] = frame
        return frames

    def from_csv(self) -> FootballDataset:
        return self.from_frames(self.read_frames())

    @staticmethod
    def player_stats_frame(dataset: FootballDataset) -> pd.DataFrame:
        """Analysis view: one player-match row with provider statistics as columns."""
        performances = PandasStore.to_frames(dataset)['performances']
        raw = pd.json_normalize(performances['raw_stats'].tolist()).add_prefix('stat.')
        return pd.concat([performances.drop(columns='raw_stats').reset_index(drop=True), raw], axis=1)

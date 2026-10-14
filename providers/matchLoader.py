"""Build shared domain objects from normalized event and lineup dictionaries."""
from copy import deepcopy
from datetime import datetime, timezone

from models import (Competition, Season, FootballDataset, Team, Player, Match,
                    Lineup, PlayerMatch, PlayerMatchStats, MatchSide, MatchStatus,
                    PlayerRoleGroup)


class MatchLoader:
    def __init__(self, client, dataset=None, role_resolver=None):
        self.client = client
        self.dataset = dataset if dataset is not None else FootballDataset()
        # Callable receiving the full lineup entry; override ambiguous midfielders.
        self.role_resolver = role_resolver

    def select_season(self, competition_id, competition_name, season_id, season_name):
        competition = self.dataset.competitions.get(competition_id)
        if competition is None:
            competition = Competition(competition_id, competition_name)
            self.dataset.add_competition(competition)
        season = competition.seasons.get(season_id)
        return season if season is not None else Season(season_id, season_name, competition)

    def iter_matches(self, season, events=None):
        """Consume supplied schedule events in kickoff order, loading one at a time.

        With no events supplied, fetch past events for the selected season.
        Explicit events can include upcoming fixtures or previously saved data.
        Existing matches are reused; request failures propagate for caller retry.
        """
        if events is None:
            events = self.client.iter_season_events(season.competition.id, season.id)
        unique = {}
        for event in events:
            if event.get('season', {}).get('id', season.id) != season.id:
                raise ValueError('Event belongs to another season')
            competition_id = event.get('tournament', {}).get('uniqueTournament', {}).get('id')
            if competition_id is not None and competition_id != season.competition.id:
                raise ValueError('Event belongs to another competition')
            unique[event['id']] = event
        for event in sorted(unique.values(), key=lambda e: (e['startTimestamp'], e['id'])):
            existing = self.dataset.matches.get(event['id'])
            if existing is not None:
                if existing.season is not season:
                    raise ValueError('Match already belongs to another season')
                yield existing
                continue
            # Fetch before registering anything: failures do not mark a match loaded.
            lineups = self.client.lineups(event['id'])
            yield self._build_match(season, event, lineups)

    def _build_match(self, season, event, payload):
        teams = []
        prepared = []
        # Validate required fields and roles before mutating shared registries.
        for side in MatchSide:
            info = event[side.value + 'Team']
            team = self.dataset.teams.get(info['id']) or Team(info['id'], info['name'])
            teams.append(team)
            entries = []
            seen = set()
            for entry in payload.get(side.value, {}).get('players', []):
                info = entry['player']
                if info['id'] in seen:
                    raise ValueError('Duplicate player in lineup')
                seen.add(info['id'])
                player = self.dataset.players.get(info['id']) or Player(info['id'], info['name'], info.get('position'))
                position = entry.get('position') or info.get('position')
                role = (self.role_resolver(entry) if self.role_resolver
                        else PlayerRoleGroup.from_position(position))
                role = PlayerRoleGroup(role)
                raw = entry.get('statistics') or {}
                entries.append((entry, player, position, role, PlayerMatchStats.from_sofascore(raw)))
            prepared.append(entries)
        status_value = event.get('status', {}).get('type', 'unknown')
        try:
            status = MatchStatus(status_value)
        except ValueError:
            status = MatchStatus.UNKNOWN
        match = Match(event['id'], season,
                      datetime.fromtimestamp(event['startTimestamp'], timezone.utc),
                      *teams, home_score=event.get('homeScore', {}).get('current'),
                      away_score=event.get('awayScore', {}).get('current'),
                      round_number=event.get('roundInfo', {}).get('round'), status=status,
                      metadata={'event': deepcopy(event), 'lineups': deepcopy(payload)})
        for side, team, entries in zip(MatchSide, teams, prepared):
            self.dataset.add_team(team)
            if side.value not in payload:
                continue  # Unavailable lineup remains None, not an empty confirmed XI.
            lineup = Lineup(match, team, side,
                            formation=payload[side.value].get('formation'),
                            confirmed=payload.get('confirmed', False))
            for entry, player, position, role, stats in entries:
                self.dataset.add_player(player)
                appearance = PlayerMatch(player, match, team, lineup,
                                         starter=entry.get('substitute') is False,
                                         position=position, shirt_number=entry.get('shirtNumber'),
                                         minutes_played=stats.raw.get('minutesPlayed'),
                                         role_group=role, stats=stats)
                lineup.add_player(appearance)
        self.dataset.add_match(match)
        return match

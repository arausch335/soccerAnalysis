from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(eq=False)
class FootballDataset:

    competitions: dict[int, Competition] = field(
        default_factory=dict
    )

    teams: dict[int, Team] = field(
        default_factory=dict
    )

    players: dict[int, Player] = field(
        default_factory=dict
    )

    matches: dict[int, Match] = field(
        default_factory=dict
    )

    def add_competition(
        self,
        competition: Competition,
    ):
        self.competitions[
            competition.id
        ] = competition

    def add_team(
        self,
        team: Team,
    ):
        self.teams[team.id] = team

    def add_player(
        self,
        player: Player,
    ):
        self.players[player.id] = player

    def add_match(
        self,
        match: Match,
    ):
        self.matches[match.id] = match

    def get_player(
        self,
        player_id: int,
    ) -> Player | None:
        return self.players.get(player_id)

    def get_team(
        self,
        team_id: int,
    ) -> Team | None:
        return self.teams.get(team_id)

    def get_match(
        self,
        match_id: int,
    ) -> Match | None:
        return self.matches.get(match_id)

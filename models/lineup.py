from __future__ import annotations
from dataclasses import dataclass, field

from .enums import MatchSide, PlayerRoleGroup


@dataclass(eq=False)
class Lineup:
    match: Match
    team: Team

    side: MatchSide

    formation: str | None = None
    confirmed: bool = False

    players: list["PlayerMatch"] = field(
        default_factory=list
    )

    def add_player(
        self,
        player_match: "PlayerMatch",
    ) -> None:
        if (player_match.lineup is not self or player_match.match is not self.match
                or player_match.team is not self.team):
            raise ValueError("Performance must belong to this lineup, team and match")
        if any(p.player.id == player_match.player.id for p in self.players):
            raise ValueError("Player already present in lineup")
        self.players.append(player_match)
        self.team.players[player_match.player.id] = player_match.player
        player_match.player.performances[self.match.id] = player_match

    @property
    def starters(self) -> list["PlayerMatch"]:
        return [
            player
            for player in self.players
            if player.starter
        ]

    @property
    def substitutes(self) -> list["PlayerMatch"]:
        return [
            player
            for player in self.players
            if not player.starter
        ]

    @property
    def attackers(self) -> list["PlayerMatch"]:
        return [
            player
            for player in self.players
            if player.role_group
            == PlayerRoleGroup.ATTACKER
        ]

    @property
    def defenders(self) -> list["PlayerMatch"]:
        return [
            player
            for player in self.players
            if player.role_group
            == PlayerRoleGroup.DEFENDER
        ]

    @property
    def goalkeeper(self) -> "PlayerMatch | None":
        return next(
            (
                player
                for player in self.players
                if player.role_group
                == PlayerRoleGroup.GOALKEEPER
            ),
            None,
        )

    def __post_init__(self):
        expected = self.match.home_team if self.side == MatchSide.HOME else self.match.away_team
        if self.team is not expected:
            raise ValueError("Lineup side does not match team")
        self.team.lineups[self.match.id] = self
        setattr(self.match, self.side.value + "_lineup", self)
        initial = list(self.players)
        self.players = []
        for performance in initial:
            self.add_player(performance)

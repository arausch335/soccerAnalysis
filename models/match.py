# models/match.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .enums import MatchStatus


@dataclass(eq=False)
class Match:
    id: int

    season: "Season"

    kickoff: datetime

    home_team: "Team"
    away_team: "Team"

    home_score: int | None = None
    away_score: int | None = None

    round_number: int | None = None

    status: MatchStatus = MatchStatus.UNKNOWN

    home_lineup: "Lineup | None" = field(
        default=None,
        repr=False,
    )

    away_lineup: "Lineup | None" = field(
        default=None,
        repr=False,
    )

    metadata: dict[str, Any] = field(
        default_factory=dict,
    )

    @property
    def teams(self):
        return (
            self.home_team,
            self.away_team,
        )

    @property
    def lineups(self):
        return (
            self.home_lineup,
            self.away_lineup,
        )

    def opponent_of(self, team: "Team") -> "Team":
        if team.id == self.home_team.id:
            return self.away_team

        if team.id == self.away_team.id:
            return self.home_team

        raise ValueError(
            f"{team.name} did not participate in match {self.id}"
        )

    def lineup_for(self, team: "Team") -> "Lineup | None":
        if team.id == self.home_team.id:
            return self.home_lineup

        if team.id == self.away_team.id:
            return self.away_lineup

        raise ValueError(
            f"{team.name} did not participate in match {self.id}"
        )
    def __post_init__(self):
        if self.home_team.id == self.away_team.id:
            raise ValueError("A match requires two distinct teams")
        if self.kickoff.tzinfo is None:
            raise ValueError("Kickoff must include a timezone")
        self.season.matches[self.id] = self
        for team in self.teams:
            team.matches[self.id] = self
            team.competitions[self.season.competition.id] = self.season.competition

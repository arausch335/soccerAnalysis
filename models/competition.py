from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(eq=False)
class Competition:
    id: int
    name: str
    seasons: dict[int, Season] = field(default_factory=dict, repr=False)

    def add_season(self, season: Season) -> None:
        if season.competition is not self:
            raise ValueError("Season belongs to another competition")
        self.seasons[season.id] = season

from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(eq=False)
class Team:
    id: int
    name: str
    players: dict[int, Player] = field(default_factory=dict, repr=False)
    competitions: dict[int, Competition] = field(default_factory=dict, repr=False)
    matches: dict[int, Match] = field(default_factory=dict, repr=False)
    lineups: dict[int, Lineup] = field(default_factory=dict, repr=False)

    @property
    def roster(self):
        """All observed players, including historical members; not a current squad."""
        return list(self.players.values())

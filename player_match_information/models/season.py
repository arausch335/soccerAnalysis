from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(eq=False)
class Season:
    id: int
    name: str
    competition: Competition
    matches: dict[int, Match] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.competition.add_season(self)

    def __iter__(self):
        return iter(sorted(self.matches.values(), key=lambda m: (m.kickoff, m.id)))

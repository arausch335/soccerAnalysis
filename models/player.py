from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(eq=False)
class Player:
    id: int
    name: str
    position: str | None = None
    information: dict[str, Any] = field(default_factory=dict)
    performances: dict[int, PlayerMatch] = field(default_factory=dict, repr=False)
    traits: dict[int, PlayerTraitSnapshot] = field(default_factory=dict, repr=False)

    def history_before(self, match: Match):
        return sorted((p for p in self.performances.values()
                       if p.match.kickoff < match.kickoff and p.did_play
                       and p.match.status.value == "finished"),
                      key=lambda p: (p.match.kickoff, p.match.id))

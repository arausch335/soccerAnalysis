from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(eq=False)
class PlayerTraitSnapshot:
    """
    Historical player traits as they existed
    BEFORE a specific target match.
    """

    player: Player

    before_match: Match

    matches_used: int

    method: str = "cumulative"

    window_size: int | None = None

    pace: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    physicality: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    passing: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    dribbling: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    shooting: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    defense: TraitCategory = field(
        default_factory=lambda: TraitCategory()
    )

    source_match_ids: list[int] = field(
        default_factory=list
    )


@dataclass(eq=False)
class TraitCategory:
    values: dict[str, float] = field(
        default_factory=dict
    )

    sample_sizes: dict[str, int] = field(
        default_factory=dict
    )

def derive_traits(player, before_match, window_size=None):
    """Derive observed completion rates, not invented ability scores.

    Only explicitly normalized actions with both counts contribute. Historical
    finished appearances strictly before kickoff prevent target/future leakage.
    """
    if window_size is not None and window_size <= 0:
        raise ValueError("window_size must be positive")
    history = player.history_before(before_match)
    if window_size is not None:
        history = history[-window_size:]
    snapshot = PlayerTraitSnapshot(player, before_match, len(history),
                                   window_size=window_size,
                                   method="rolling" if window_size else "cumulative",
                                   source_match_ids=[p.match.id for p in history])
    categories = {"passing": snapshot.passing, "dribbling": snapshot.dribbling,
                  "shooting": snapshot.shooting, "defense": snapshot.defense}
    for name, category in categories.items():
        counts = [p.stats.actions[name] for p in history if name in p.stats.actions]
        counts = [c for c in counts if c.attempts is not None and c.completions is not None]
        attempts = sum(c.attempts for c in counts)
        if attempts:
            category.values["completion_rate"] = sum(c.completions for c in counts) / attempts
            category.sample_sizes["completion_rate"] = attempts
    player.traits[before_match.id] = snapshot
    return snapshot

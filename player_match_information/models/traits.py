"""Legacy snapshot data types retained for reading existing checkpoints."""
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

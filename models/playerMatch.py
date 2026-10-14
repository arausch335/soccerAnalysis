from __future__ import annotations
from dataclasses import dataclass, field

from .enums import PlayerRoleGroup
from .stats import PlayerMatchStats


@dataclass(eq=False)
class PlayerMatch:
    player: Player
    match: Match
    team: Team
    lineup: Lineup

    starter: bool = False

    position: str | None = None
    shirt_number: int | None = None

    minutes_played: int | None = None

    role_group: PlayerRoleGroup = (
        PlayerRoleGroup.UNKNOWN
    )

    stats: PlayerMatchStats = field(
        default_factory=PlayerMatchStats
    )

    @property
    def opponent(self) -> Team:
        return self.match.opponent_of(self.team)

    @property
    def did_play(self) -> bool:
        if self.minutes_played is not None and self.minutes_played > 0:
            return True
        # A provider can confirm an appearance without publishing comparable minutes.
        return any(source.get('appeared', False)
                   for source in self.stats.raw.get('_providers', {}).values())

    @property
    def is_attacker(self) -> bool:
        return (
            self.role_group
            == PlayerRoleGroup.ATTACKER
        )

    @property
    def is_defender(self) -> bool:
        return (
            self.role_group
            == PlayerRoleGroup.DEFENDER
        )

    @property
    def is_goalkeeper(self) -> bool:
        return (
            self.role_group
            == PlayerRoleGroup.GOALKEEPER
        )

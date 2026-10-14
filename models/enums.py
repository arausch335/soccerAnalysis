from enum import Enum

class MatchSide(str, Enum):
    HOME = "home"
    AWAY = "away"

class MatchStatus(str, Enum):
    UNKNOWN = "unknown"
    NOT_STARTED = "notstarted"
    IN_PROGRESS = "inprogress"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELED = "canceled"

class PlayerRoleGroup(str, Enum):
    ATTACKER = "attacker"
    DEFENDER = "defender"
    GOALKEEPER = "goalkeeper"
    UNKNOWN = "unknown"

    @classmethod
    def from_position(cls, position):
        # Midfield roles require an explicit override; position M alone is ambiguous.
        return {"F": cls.ATTACKER, "D": cls.DEFENDER,
                "G": cls.GOALKEEPER}.get(position, cls.UNKNOWN)

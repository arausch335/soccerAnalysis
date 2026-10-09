from dataclasses import dataclass, field
from typing import Any
from copy import deepcopy

@dataclass
class AttemptCompletion:
    attempts: int | None = None
    completions: int | None = None

    def __post_init__(self):
        for value in (self.attempts, self.completions):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError("Counts must be nonnegative integers or None")
        if self.attempts is not None and self.completions is not None:
            if self.completions > self.attempts:
                raise ValueError("Completions cannot exceed attempts")

    @property
    def rate(self):
        if not self.attempts or self.completions is None:
            return None
        return self.completions / self.attempts

@dataclass
class PlayerMatchStats:
    raw: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, AttemptCompletion] = field(default_factory=dict)

    @classmethod
    def from_sofascore(cls, values):
        # Preserve unknown fields and distinguish absent values from zero.
        return cls(raw=deepcopy(values))

    def record_action(self, name, attempts=None, completions=None):
        self.actions[name] = AttemptCompletion(attempts, completions)

"""Pure contracts for sampled actions, group rewards and conservative spending."""

from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite

MIN_GROUP_SIZE = 2


@dataclass(frozen=True, slots=True)
class Turn:
    """Tokens exactly as submitted and returned by one frozen policy."""

    prompt: tuple[int, ...]
    completion: tuple[int, ...]
    logprobs: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.prompt or not self.completion:
            raise ValueError("empty prompt or completion")
        if len(self.completion) != len(self.logprobs):
            raise ValueError("completion/logprob length mismatch")
        if not all(isfinite(x) for x in self.logprobs):
            raise ValueError("nonfinite logprob")
        if any(x < 0 for x in (*self.prompt, *self.completion)):
            raise ValueError("negative token id")


def training_arrays(
    turn: Turn, advantage: float
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[float, ...], tuple[float, ...]]:
    """Right-shift targets; historical actions and observations have zero loss."""
    if not isfinite(advantage):
        raise ValueError("nonfinite advantage")
    tokens = turn.prompt + turn.completion
    prefix = (0.0,) * (len(turn.prompt) - 1)
    return (
        tokens[:-1],
        tokens[1:],
        prefix + turn.logprobs,
        prefix + (advantage,) * len(turn.completion),
    )


def advantages(rewards: tuple[float, ...]) -> tuple[float, ...]:
    """Group centering without variance amplification or resampling."""
    if len(rewards) < MIN_GROUP_SIZE or not all(isfinite(x) for x in rewards):
        raise ValueError("invalid reward group")
    mean = sum(rewards) / len(rewards)
    return tuple(x - mean for x in rewards)


@dataclass(slots=True)
class Ledger:
    """Reserve a known upper bound before submission; unknown RPCs stay held."""

    limit: Decimal
    spent: Decimal = Decimal("0")
    pending: dict[str, Decimal] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)

    @property
    def available(self) -> Decimal:
        return self.limit - self.spent - sum(self.pending.values(), Decimal("0"))

    def reserve(self, operation: str, maximum: Decimal) -> None:
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError("invalid reservation")
        if operation in self.pending or operation in self.settled:
            raise ValueError("duplicate operation")
        if maximum > self.available:
            raise ValueError("budget exhausted")
        self.pending[operation] = maximum

    def settle(self, operation: str, actual: Decimal) -> None:
        if not actual.is_finite() or actual < 0 or operation not in self.pending:
            raise ValueError("invalid settlement")
        del self.pending[operation]
        self.spent += actual
        self.settled.add(operation)

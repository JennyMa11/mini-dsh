"""Per-study cap over the single existing financial epoch, never a fresh ledger."""

from decimal import Decimal
from pathlib import Path
from typing import Protocol

from minidsh_rl.backend_v2 import SamplingBudget
from minidsh_rl.development_schedule import CAP


class FinancialPort(SamplingBudget, Protocol):
    @property
    def committed(self) -> Decimal: ...


class StudyBudget:
    def __init__(self, parent: FinancialPort, baseline: Decimal) -> None:
        if baseline > parent.committed:
            raise ValueError("study baseline exceeds actual commitment")
        self.parent, self.baseline = parent, baseline

    @property
    def path(self) -> Path:
        return self.parent.path

    @property
    def committed(self) -> Decimal:
        return self.parent.committed

    @property
    def available(self) -> Decimal:
        return min(self.parent.available, CAP - (self.committed - self.baseline))

    def reserve(self, operation: str, maximum: Decimal) -> None:
        if maximum > self.available:
            raise ValueError("complete study cap would be exceeded")
        self.parent.reserve(operation, maximum)

    def settle(self, operation: str, estimate: Decimal) -> None:
        self.parent.settle(operation, estimate)

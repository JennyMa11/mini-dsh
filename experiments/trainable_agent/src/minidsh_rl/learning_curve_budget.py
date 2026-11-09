"""Sixty-CNY study ceiling inside the existing immutable 150/100 epoch."""

from decimal import Decimal
from pathlib import Path

from minidsh_rl.curve_budget import CurveBudget

CAP = Decimal("60")


class LearningCurveBudget(CurveBudget):
    def __init__(self, directory: Path, baseline: Decimal) -> None:
        self.curve_baseline = baseline
        super().__init__(directory)
        if baseline > self.committed:
            self.close()
            raise ValueError("curve baseline exceeds actual financial commitment")

    @property
    def available(self) -> Decimal:
        return min(super().available, CAP - (self.committed - self.curve_baseline))

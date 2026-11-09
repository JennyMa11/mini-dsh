"""Study60 retains the existing epoch's spending and unknown reservations."""

from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.curve_budget import CurveBudget, prepare_migration
from minidsh_rl.learning_curve_budget import LearningCurveBudget
from test_calibrated_budget import fixture


def test_study_limit_does_not_reset_native_epoch(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "parent.json"
    with CalibratedBudget(parent, audit=audit):
        pass
    directory = tmp_path / "finance"
    prepare_migration(parent, audit, directory)
    with CurveBudget(directory) as native:
        native.reserve("previous_study", Decimal("2"))
        native.settle("previous_study", Decimal("2"))
        baseline = native.committed
    with LearningCurveBudget(directory, baseline) as scoped:
        assert scoped.available == Decimal("60")
        scoped.reserve("unknown_curve_rpc", Decimal("59"))
    with LearningCurveBudget(directory, baseline) as scoped:
        assert scoped.available == Decimal("1")
        with pytest.raises(ValueError, match="allocation"):
            scoped.reserve("over_cap", Decimal("1.001"))
        with pytest.raises(ValueError, match="duplicate"):
            scoped.reserve("unknown_curve_rpc", Decimal(".01"))

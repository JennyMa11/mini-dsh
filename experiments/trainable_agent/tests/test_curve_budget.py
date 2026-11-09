"""Explicit limit increase must preserve history and unknown operations."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.curve_budget import CurveBudget, prepare_migration
from test_calibrated_budget import fixture


def test_migration_preserves_history_and_caps_new_spending(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "budget.json"
    with CalibratedBudget(parent, audit=audit) as old:
        old.reserve("recent", Decimal("1"))
        old.settle("recent", Decimal(".5"))
    before = parent.read_bytes()
    target = tmp_path / "curve"
    prepare_migration(parent, audit, target)
    with CurveBudget(target) as budget:
        assert budget.committed == Decimal("16.5")
        assert budget.available == Decimal("100")
        for operation in ("recent", "old_rpc"):
            with pytest.raises(ValueError, match="historical"):
                budget.reserve(operation, Decimal(".01"))
        budget.reserve("new", Decimal("99"))
    with CurveBudget(target) as budget:
        assert budget.available == Decimal("1")
        with pytest.raises(ValueError, match="allocation"):
            budget.reserve("too_large", Decimal("1.01"))
        with pytest.raises(ValueError, match="duplicate"):
            budget.reserve("new", Decimal("1"))
    assert parent.read_bytes() == before


def test_unknown_parent_and_changed_ancestry_refused(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "budget.json"
    with CalibratedBudget(parent, audit=audit) as old:
        old.reserve("unknown", Decimal("1"))
    with pytest.raises(ValueError, match="pending"):
        prepare_migration(parent, audit, tmp_path / "bad")


def test_parent_edit_refuses_new_requests(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "budget.json"
    with CalibratedBudget(parent, audit=audit):
        pass
    target = tmp_path / "curve"
    prepare_migration(parent, audit, target)
    with CurveBudget(target) as budget:
        (tmp_path / "bill.json").write_text("changed")
        with pytest.raises(ValueError, match="evidence"):
            budget.reserve("new", Decimal(".01"))


def test_forged_lower_baseline_is_not_a_limit_increase(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "budget.json"
    with CalibratedBudget(parent, audit=audit):
        pass
    target = tmp_path / "curve"
    prepare_migration(parent, audit, target)
    path = target / "migration.json"
    record = json.loads(path.read_text())
    record["baseline_committed"] = "0"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="baseline"):
        CurveBudget(target)


def test_one_parent_cannot_fund_two_independent_epochs(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    parent = tmp_path / "budget.json"
    with CalibratedBudget(parent, audit=audit):
        pass
    prepare_migration(parent, audit, tmp_path / "first")
    with pytest.raises(ValueError, match="already"):
        prepare_migration(parent, audit, tmp_path / "second")
    with pytest.raises(ValueError, match="sealed"):
        CalibratedBudget(parent, audit=audit)

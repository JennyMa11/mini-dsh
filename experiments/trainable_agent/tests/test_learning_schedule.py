"""Full predeclared queues and whole-stage resources are independent of outcomes."""

from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl import learning_schedule
from minidsh_rl.learning_schedule import complete_allocation, load_plan, make_plan, prepare


def test_plan_keeps_all_eighty_attempts_and_matched_test_pairs() -> None:
    plan = make_plan("contract_fixture", experiment_id="unit")
    assert len(plan.rows) == 80
    assert len({r.run_id for r in plan.rows}) == 80
    assert sum(r.phase == "readiness" for r in plan.rows) == 16
    assert sum(r.phase == "train" for r in plan.rows) == 32
    test = [r for r in plan.rows if r.phase == "test"]
    assert len(test) == 32
    for a, b in zip(test[::2], test[1::2], strict=True):
        assert (a.case_id, a.seed) == (b.case_id, b.seed)
        assert {a.policy, b.policy} == {"base", "rl"}
    assert complete_allocation() == Decimal("20.302")
    assert plan.required_cny == complete_allocation()


def test_training_groups_have_fixed_four_unique_seeds_and_one_case() -> None:
    plan = make_plan("contract_fixture", experiment_id="unit")
    groups = {row.group for row in plan.rows if row.phase == "train"}
    assert len(groups) == 8
    for group in groups:
        rows = [row for row in plan.rows if row.phase == "train" and row.group == group]
        assert len(rows) == 4
        assert len({r.case_id for r in rows}) == 1
        assert len({r.seed for r in rows}) == 4


def test_funding_modes_preserve_identical_scientific_queue() -> None:
    original = make_plan("contract_fixture", experiment_id="unit")
    staged = make_plan(
        "contract_fixture", experiment_id="unit", funding_policy="confirmed_stage_v1"
    )
    assert original.funding_policy == "full_remaining_v1"
    assert original.rows == staged.rows
    assert original.case_sha256 == staged.case_sha256
    assert original.required_cny == staged.required_cny == Decimal("20.302")


@pytest.mark.parametrize("change", ["added", "removed"])
def test_source_inventory_change_invalidates_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory = tmp_path / "study"
    plan = prepare(directory, kind="contract_fixture")
    inventory = dict(plan.sources)
    if change == "added":
        inventory[str(tmp_path / "new_adapter.py")] = "a" * 64
    else:
        inventory.pop(next(iter(inventory)))
    monkeypatch.setattr(learning_schedule, "_sources", lambda: inventory)
    with pytest.raises(ValueError, match="source"):
        load_plan(directory)

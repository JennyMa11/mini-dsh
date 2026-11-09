"""Full funding precedes provider factories; incomplete evidence retains consumption."""

import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from minidsh_rl import learning_curve_run
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.curve_analysis import analyze_curve
from minidsh_rl.curve_budget import CurveBudget, prepare_migration
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.curve_schedule import make_curve_plan
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_schedule import atomic_record, digest
from test_calibrated_budget import fixture


def financial_fixture(root: Path) -> Path:
    audit = fixture(root)
    parent = root / "budget.json"
    with CalibratedBudget(parent, audit=audit):
        pass
    target = root / "finance"
    prepare_migration(parent, audit, target)
    with CurveBudget(target):
        pass
    return target


async def test_insufficient_full_queue_funding_does_not_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finance = financial_fixture(tmp_path)
    with CurveBudget(finance) as budget:
        baseline = budget.committed
        budget.reserve("old_unknown", Decimal("99"))
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={"finance_directory": str(finance), "budget_baseline": baseline}
    )
    monkeypatch.setattr(
        learning_curve_run, "load_learning_curve", lambda _path: (plan, cast(ExecutionConfig, None))
    )

    async def forbidden(_path: Path) -> None:
        raise AssertionError("provider preflight must not happen")

    monkeypatch.setattr(learning_curve_run, "service_evidence", forbidden)
    with pytest.raises(ValueError, match="complete remaining"):
        await learning_curve_run.run_learning_curve(tmp_path)
    assert not (tmp_path / "live-preflight").exists()
    with CurveBudget(finance) as budget:
        assert budget.available == Decimal("1")


def test_uncommitted_sample_preserves_tokens_and_unknown_calls(tmp_path: Path) -> None:
    finance = financial_fixture(tmp_path)
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={"finance_directory": str(finance), "budget_baseline": Decimal("16")}
    )
    atomic_record(tmp_path / "plan.json", plan, create=True)
    row = plan.rows[0]
    state = CurveState(
        plan_sha256=digest(tmp_path / "plan.json"),
        kind="provider",
        pending="sample",
        key=row.run_id,
    )
    atomic_record(tmp_path / "curve-state.json", state, create=True)
    folder = tmp_path / "rollouts" / row.run_id
    (folder / "financial-calls").mkdir(parents=True)
    (folder / "sample-started").mkdir()
    (folder / "financial-calls/turn_000.json").write_text(
        json.dumps({"input_tokens": 20, "output_tokens": 3})
    )
    for index in range(2):
        (folder / "sample-started" / f"turn_{index:03}.json").write_text("{}")
    result = analyze_curve(tmp_path, tmp_path / "derived.json")
    assert result["known_rollout_tokens"] == 23
    assert result["new_provider_calls"] == 1
    assert result["unknown_provider_calls"] == 1
    assert result["completed_trajectories"] == 0
    assert result["declared_trajectories"] == 192
    assert result["paired_success_delta"] is None
    assert not result["learning_gate_passed"]

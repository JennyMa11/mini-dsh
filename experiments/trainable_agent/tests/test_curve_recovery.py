"""Only a terminal pre-sampling initialization can retain its hold across a new run."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.curve_budget import CurveBudget
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.curve_recovery import (
    FencedResources,
    freeze_failed_restore,
    freeze_failure,
    resources_for,
    validate_failure,
)
from minidsh_rl.curve_schedule import make_curve_plan
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.learning_sdk import LeasedResources
from test_learning_curve_admission import financial_fixture


def failed_fixture(root: Path) -> tuple[Path, Path, Path]:
    finance = financial_fixture(root)
    old = root / "old"
    old.mkdir()
    with CurveBudget(finance) as budget:
        baseline = budget.committed
        (old / "budget-before.json").write_bytes(budget.path.read_bytes())
        scope = hashlib.sha256(str((old / "initial-sdk").resolve()).encode()).hexdigest()[:16]
        operation = f"initial-sdk_{scope}_state_initial"
        budget.reserve(operation, Decimal(".10"))
        (old / "budget-after.json").write_bytes(budget.path.read_bytes())
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={"finance_directory": str(finance.resolve()), "budget_baseline": baseline}
    )
    atomic_record(old / "plan.json", plan, create=True)
    atomic_record(
        old / "curve-state.json",
        CurveState(plan_sha256=digest(old / "plan.json"), kind="provider", pending="initialize"),
        create=True,
    )
    (old / "stop-001.json").write_text(
        json.dumps(
            {
                "error_type": "ActorEventSubmitError",
                "automatic_retry": False,
                "frames": [{"function": "_submit_init"}, {"function": "_request"}],
            }
        )
    )
    (old / "source-checkpoint.json").write_text(
        json.dumps({"optimizer_uri": "trio://original/state"})
    )
    metadata = root / "owned.json"
    metadata.write_text(
        json.dumps(
            {
                "training_run_cuid": "run_old",
                "owned": True,
                "status": "ENDED",
                "checkpoint_count": 0,
                "restore_optimizer": True,
                "source_optimizer": "trio://original/state",
                "model": "Qwen/Qwen3.5-4B",
                "lora_rank": 16,
                "seed": 17,
            }
        )
    )
    return old, finance, metadata


def test_fence_retains_amount_and_blocks_every_new_pending(tmp_path: Path) -> None:
    old, finance, metadata = failed_fixture(tmp_path)
    proof = tmp_path / "fence.json"
    fence = freeze_failure(old, metadata, proof)
    with CurveBudget(finance) as budget:
        before = budget.path.read_bytes()
        resources = FencedResources(budget, proof)
        assert LeasedResources(budget).has_pending
        assert not bool(resources.has_pending)
        assert resources.committed == Decimal("16.10")
        assert budget.path.read_bytes() == before
        budget.reserve("new_unknown", Decimal(".01"))
        assert bool(resources.has_pending)
    assert validate_failure(proof) == fence


@pytest.mark.parametrize(
    "change", ["completed", "remote_running", "remote_checkpoint", "changed_source"]
)
def test_noninitial_or_changed_remote_evidence_is_not_fenced(tmp_path: Path, change: str) -> None:
    old, _finance, metadata = failed_fixture(tmp_path)
    if change == "completed":
        path = old / "curve-state.json"
        value = json.loads(path.read_text())
        value["completed"] = ["sample"]
        path.write_text(json.dumps(value))
    else:
        value = json.loads(metadata.read_text())
        value.update(
            {"status": "RUNNING"}
            if change == "remote_running"
            else {"checkpoint_count": 1}
            if change == "remote_checkpoint"
            else {"source_optimizer": "trio://different/state"}
        )
        metadata.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="terminal pre-sampling"):
        freeze_failure(old, metadata, tmp_path / "fence.json")


def test_changed_evidence_refuses_even_the_old_pending_exception(tmp_path: Path) -> None:
    old, finance, metadata = failed_fixture(tmp_path)
    proof = tmp_path / "fence.json"
    freeze_failure(old, metadata, proof)
    metadata.write_text("{}")
    with CurveBudget(finance) as budget, pytest.raises(ValueError, match="changed"):
        _ = FencedResources(budget, proof).has_pending


def test_one_claim_and_original_study_baseline_required(tmp_path: Path) -> None:
    old, finance, metadata = failed_fixture(tmp_path)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    path = attempt / "recovery-evidence.json"
    proof = freeze_failure(old, metadata, path)
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={
            "finance_directory": str(finance),
            "budget_baseline": proof.original_baseline,
            "recovery_sha256": digest(path),
        }
    )
    (old / "recovery-claim.json").write_text(
        json.dumps({"recovery_root": str(attempt.resolve()), "evidence_sha256": digest(path)})
    )
    with CurveBudget(finance) as budget:
        assert not resources_for(attempt, plan, budget).has_pending
        with pytest.raises(ValueError, match="reset original"):
            resources_for(
                attempt, plan.model_copy(update={"budget_baseline": Decimal("16.10")}), budget
            )
        value = json.loads((old / "recovery-claim.json").read_text())
        value["recovery_root"] = str(tmp_path / "second")
        (old / "recovery-claim.json").write_text(json.dumps(value))
        with pytest.raises(ValueError, match="claimed"):
            resources_for(attempt, plan, budget)


def test_verified_fresh_path_keeps_both_init_holds_and_cannot_repeat(tmp_path: Path) -> None:
    old, finance, metadata = failed_fixture(tmp_path)
    second = tmp_path / "second"
    second.mkdir()
    first_proof = second / "recovery-evidence.json"
    freeze_failure(old, metadata, first_proof)
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={
            "finance_directory": str(finance.resolve()),
            "budget_baseline": Decimal("16"),
            "recovery_sha256": digest(first_proof),
        }
    )
    atomic_record(second / "plan.json", plan, create=True)
    atomic_record(
        second / "curve-state.json",
        CurveState(plan_sha256=digest(second / "plan.json"), kind="provider", pending="initialize"),
        create=True,
    )
    for name in ("source-checkpoint.json", "stop-001.json"):
        (second / name).write_bytes((old / name).read_bytes())
    (second / "initial-sdk").mkdir()
    (second / "initial-sdk/registered-client.json").write_text(
        json.dumps({"training_run_cuid": "run_second"})
    )
    (second / "initial-sdk/state_initial-error.json").write_text(
        json.dumps({"status_code": 404, "code": "not_found"})
    )
    scope = hashlib.sha256(str((second / "initial-sdk").resolve()).encode()).hexdigest()[:16]
    with CurveBudget(finance) as budget:
        (second / "budget-before.json").write_bytes(budget.path.read_bytes())
        budget.reserve(f"initial-sdk_{scope}_state_initial", Decimal(".10"))
        (second / "budget-after.json").write_bytes(budget.path.read_bytes())
    owned = json.loads(metadata.read_text())
    owned["training_run_cuid"] = "run_second"
    new_metadata = tmp_path / "second-owned.json"
    new_metadata.write_text(json.dumps(owned))
    proof = tmp_path / "fresh-proof.json"
    value = freeze_failed_restore(second, new_metadata, proof)
    assert len(value.prior_holds) == 1
    with CurveBudget(finance) as budget:
        assert not FencedResources(budget, proof).has_pending
        assert budget.committed == Decimal("16.20")
    plan = plan.model_copy(update={"initialization": "verified_fresh_seed17"})
    atomic_record(second / "plan.json", plan)
    with pytest.raises(ValueError, match="changed"):
        validate_failure(proof)

"""Apply the frozen budget-priority amendment without using validation direction."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from minidsh_rl.billing_calibration import ROOT
from minidsh_rl.calibrated_budget import CalibratedBudget

TEST_ALLOCATION = Decimal("7.68")
PILOT_SLOTS = 40
VALIDATION_RUNS = 64
TEST_RUNS = 128


def resource_admission(  # noqa: PLR0913 - explicit completeness and resource criteria
    *,
    slots: int,
    validation_runs: int,
    audited_slots: int,
    nonzero_updates: int,
    pending: bool,
    available: Decimal,
) -> bool:
    """Improvement is deliberately absent from this rule's inputs."""
    return (
        slots == audited_slots == PILOT_SLOTS
        and validation_runs == VALIDATION_RUNS
        and nonzero_updates > 0
        and not pending
        and available >= TEST_ALLOCATION
    )


def decide(
    training: Path, validation: Path, amendment: Path, trajectory: Path
) -> dict[str, object]:
    """Bind complete audit, frozen 40-slot checkpoint and cumulative budget evidence."""
    frozen = json.loads(amendment.read_text())
    if (
        frozen["training_final_slot"] != PILOT_SLOTS
        or frozen["final_test_runs"] != TEST_RUNS
        or frozen["test_depends_on_validation_improvement"] is not False
        or frozen["new_test_model_calls_at_freeze"] != 0
        or Decimal(frozen["cumulative_cap_cny"]) != Decimal("50")
    ):
        raise ValueError("unexpected frozen resource protocol")
    evidence = Path(frozen["resource_evidence"])
    if hashlib.sha256(evidence.read_bytes()).hexdigest() != frozen["resource_evidence_sha256"]:
        raise ValueError("resource evidence changed")
    proof_path = Path(frozen["bound_proof"])
    if hashlib.sha256(proof_path.read_bytes()).hexdigest() != frozen["bound_proof_sha256"]:
        raise ValueError("test bound proof changed")
    proof = json.loads(proof_path.read_text())
    if Decimal(proof["final_test_allocation_cny"]) != TEST_ALLOCATION:
        raise ValueError("test allocation differs from bound proof")
    for source, digest in proof["sources"].items():
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != digest:
            raise ValueError("test bound source changed")
    summary_path = training / "summary.json"
    summary = json.loads(summary_path.read_text())
    parent_hash = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    trajectory_report = json.loads(trajectory.read_text())
    if (
        trajectory_report["passed"] is not True
        or trajectory_report["training_summary_sha256"] != parent_hash
        or trajectory_report["audited_rollouts"] != PILOT_SLOTS * 4
        or trajectory_report["unknown_provider_failures"] != 0
    ):
        raise ValueError("complete training trajectory audit is required")
    report = json.loads(validation.read_text())
    if (
        report["parent_summary_sha256"] != parent_hash
        or report["split"] != "validation"
        or report["declared"] != VALIDATION_RUNS
        or summary["declared_slots"] != PILOT_SLOTS
        or summary["completed_until_slot"] != PILOT_SLOTS
    ):
        raise ValueError("validation report/checkpoint mismatch")
    audit = report["training_audit"]
    with CalibratedBudget(
        ROOT / "expansion-v2/calibrated-budget.json",
        audit=ROOT / "expansion-v2/reconciliation-01/audit.json",
    ) as budget:
        state = json.loads(budget.path.read_text())
        pending = any(c["status"] == "pending" for c in state["charges"].values())
        allowed = resource_admission(
            slots=summary["completed_slots"],
            validation_runs=report["completed"],
            audited_slots=audit["audited_slots"],
            nonzero_updates=audit["nonzero_updates"],
            pending=pending,
            available=budget.available,
        )
        return {
            "allow_final_test": allowed,
            "allow_extension": False,
            "reason": "fixed_40_slot_test_admitted" if allowed else "resource_or_completeness_gate",
            "parent_summary_sha256": parent_hash,
            "training": str(training),
            "validation_report": str(validation),
            "validation_report_sha256": hashlib.sha256(validation.read_bytes()).hexdigest(),
            "amendment_sha256": hashlib.sha256(amendment.read_bytes()).hexdigest(),
            "available_cny_at_admission": str(budget.available),
            "test_allocation_cny": str(TEST_ALLOCATION),
            "validation_improvement_used_for_admission": False,
            "trajectory_audit": str(trajectory),
            "trajectory_audit_sha256": hashlib.sha256(trajectory.read_bytes()).hexdigest(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--validation-report", type=Path, required=True)
    parser.add_argument("--amendment", type=Path, required=True)
    parser.add_argument("--trajectory-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = decide(args.training, args.validation_report, args.amendment, args.trajectory_audit)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()

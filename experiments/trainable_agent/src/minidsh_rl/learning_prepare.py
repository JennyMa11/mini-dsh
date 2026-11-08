"""Local preparation/admission report only; deliberately no provider execute command."""

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from minidsh_rl.cost_store import _restore, _State
from minidsh_rl.learning_execution import (
    ExecutionState,
    next_stage_allocation,
    remaining_allocation,
    validate_state,
)
from minidsh_rl.learning_schedule import digest, load_plan, prepare


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Read-only dry-run input, not a substitute for a leased live spending port."""

    available: Decimal
    committed: Decimal
    has_pending: bool


def read_resources(ledger: Path) -> ResourceSnapshot:
    account = _restore(_State.model_validate_json(ledger.read_bytes()))
    return ResourceSnapshot(
        account.available,
        account.committed,
        any(charge.status == "pending" for charge in account.charges.values()),
    )


def admission(directory: Path, ledger: Path) -> dict[str, object]:
    """Inspect fixed remaining work without creating a client or changing the ledger."""
    plan = load_plan(directory)
    state_path = directory / "state.json"
    state = (
        ExecutionState.model_validate_json(state_path.read_bytes())
        if state_path.exists()
        else ExecutionState(
            plan_sha256=digest(directory / "plan.json"),
            rows={r.run_id: "not_run" for r in plan.rows},
        )
    )
    resources = read_resources(ledger)
    validate_state(plan, state, digest(directory / "plan.json"))
    needed = remaining_allocation(plan, state)
    next_needed = next_stage_allocation(plan, state)
    return {
        "schema_version": 1,
        "execution_admitted": False,
        "financial_capacity": not resources.has_pending and resources.available >= needed,
        "next_stage_financial_capacity": not resources.has_pending
        and resources.available >= next_needed,
        "funding_policy": plan.funding_policy,
        "reason": "offline_preparation_only_provider_adapter_not_connected",
        "required_remaining_cny": str(needed),
        "next_stage_required_cny": str(next_needed),
        "available_cny": str(resources.available),
        "committed_cny": str(resources.committed),
        "shortfall_cny": str(max(Decimal(0), needed - resources.available)),
        "pending_financial_operations": resources.has_pending,
        "controller_pending": state.pending_kind,
        "declared_rows": len(plan.rows),
        "completed_rows": sum(value == "completed" for value in state.rows.values()),
        "ledger_sha256": digest(ledger),
        "plan_sha256": digest(directory / "plan.json"),
        "new_provider_calls": 0,
        "new_training_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admission-only", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--data-evidence",
        type=Path,
        default=Path(
            "benchmark-results/trainable_agent/learning-fixtures-v1/metadata-v2/manifest.json"
        ),
    )
    parser.add_argument(
        "--budget-ledger",
        type=Path,
        default=Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json"),
    )
    args = parser.parse_args()
    if not args.admission_only:
        prepare(args.output, data_evidence=args.data_evidence)
    report = admission(args.output, args.budget_ledger)
    if args.report is not None:
        with args.report.open("x") as stream:
            json.dump(report, stream, indent=2)
    sys.stdout.write(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()

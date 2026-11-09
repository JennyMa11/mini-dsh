"""Read-only sampling-only admission metrics and explicit historical limitations."""

import argparse
import json
from pathlib import Path

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.cost_store import _restore, _State
from minidsh_rl.curve_analysis import Outcome, checkpoint_metrics
from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.development_cases import development_cases
from minidsh_rl.development_execution import DevelopmentState
from minidsh_rl.development_schedule import DevelopmentPlan
from minidsh_rl.learning_rollout import load_rollout
from minidsh_rl.learning_schedule import digest


def analyze_development(root: Path, output: Path) -> dict[str, object]:
    """All raw files remain immutable, and no candidates or checkers are executed."""
    plan = DevelopmentPlan.model_validate_json((root / "plan.json").read_bytes())
    state = DevelopmentState.model_validate_json((root / "state.json").read_bytes())
    if (
        plan.kind != "provider"
        or state.plan_sha256 != digest(root / "plan.json")
        or plan.start_committed is None
    ):
        raise ValueError("real frozen provider study required")
    verify_hashes(root, state.artifacts)
    cases = {case.task.case_id: case for case in development_cases()}
    outcomes: list[Outcome] = []
    trajectories: list[dict[str, object]] = []
    started = returned = 0
    for row in plan.rows:
        folder = root / "rollouts" / row.run_id
        started += len(list((folder / "sample-started").glob("turn_*.json")))
        returned += len(list((folder / "sample-returned").glob("turn_*.json")))
        if row.run_id not in state.completed:
            trajectories.append({"run_id": row.run_id, "status": "not_run", "declared": True})
            continue
        value = load_rollout(cases[row.case_id], folder, state.policy)
        raw = json.loads((folder / "result.json").read_text())
        trace = TraceRun.from_file(folder / "trace.jsonl", run_id=row.run_id)
        finance = sorted((folder / "financial-calls").glob("turn_*.json"))
        if len(finance) != len(value.turns) or row.seed != value.seed or row.run_id != value.run_id:
            raise ValueError("queue/sample/financial binding mismatch")
        for path, turn in zip(finance, value.turns, strict=True):
            record = json.loads(path.read_text())
            if record["input_tokens"] != len(turn.prompt) or record["output_tokens"] != len(
                turn.completion
            ):
                raise ValueError("financial usage mismatch")
        outcome: Outcome = {
            "success": value.success,
            "tokens": value.total_tokens,
            "tools": raw["tool_calls"],
            "latency": raw["duration_ms"],
            "format_rejections": len(trace.by_name("model_output_rejected")),
            "sampled": len(value.turns),
            "executed": value.verification_executed,
            "surprisal_sum": -sum(sum(turn.logprobs) for turn in value.turns),
            "action_tokens": sum(len(turn.completion) for turn in value.turns),
        }
        outcomes.append(outcome)
        trajectories.append(
            {
                "run_id": row.run_id,
                "case_id": row.case_id,
                "seed": row.seed,
                "family_group": value.case.family_group,
                "group": row.group,
                "status": "completed",
                "loop_status": value.status,
                **outcome,
            }
        )
    mixed = [group for group, rewards in state.rewards.items() if len(set(rewards)) > 1]
    ledger_path = Path(plan.finance_directory) / "budget.json"
    ledger = _State.model_validate_json(ledger_path.read_bytes())
    current = _restore(ledger)
    snapshot = _State.model_validate_json((root / "budget-after.json").read_bytes())
    study_end = _restore(snapshot)
    pending = [key for key, charge in ledger.charges.items() if charge.status == "pending"]
    report: dict[str, object] = {
        "schema_version": 1,
        "dataset_version": plan.dataset_version,
        "phase": state.phase,
        "declared": len(plan.rows),
        "completed": len(state.completed),
        "metrics": checkpoint_metrics(outcomes),
        "rewards": state.rewards,
        "group_families": state.families,
        "mixed_groups": len(mixed),
        "mixed_family_groups": sorted({state.families[group] for group in mixed}),
        "feedback_family_groups": state.recovered_families,
        "sampling_started": started,
        "sampling_returned": returned,
        "unknown_provider_calls": started - returned,
        "training_updates": 0,
        "new_training_clients": 0,
        "new_checkpoints": 0,
        "pending_run": state.pending,
        "pending_operations": pending,
        "study_committed_cny": str(study_end.committed - plan.start_committed),
        "cumulative_at_completion_cny": str(study_end.committed),
        "current_cumulative_committed_cny": str(current.committed),
        "source_plan_sha256": digest(root / "plan.json"),
        "source_state_sha256": digest(root / "state.json"),
        "generator_sha256": digest(Path(__file__)),
        "trajectories": trajectories,
        "limitations": [
            "Six already opened development cases, no held-out generalization claim.",
            "Public inputs and diagnostics were revised together; no isolated format effect.",
            "Same historical initial weights and sampling seeds, without a concurrent v2 arm.",
            "Passing readiness is not RL benefit; this entry has no training path.",
            "Sampled surprisal is not full policy entropy.",
            "Financial values are conservative commitments, not final paid invoices.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    analyze_development(args.directory, args.output)


if __name__ == "__main__":
    main()

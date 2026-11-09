"""Read-only curve/stop reporting from sealed trajectories; no model clients."""

import argparse
import json
import math
import random
from pathlib import Path
from typing import TypedDict

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.cost_store import _restore, _State
from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.curve_budget import Migration
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.curve_recovery import validate_failure
from minidsh_rl.curve_schedule import CHECKPOINTS, MIN_GROUPS, CurvePlan, curve_cases_for
from minidsh_rl.learning_execution import GroupRecord
from minidsh_rl.learning_rollout import load_rollout
from minidsh_rl.learning_schedule import digest

DECLARED_PER_POINT = 24
REPEATS = 10000


class Outcome(TypedDict):
    success: bool
    tokens: int
    tools: int
    latency: float
    format_rejections: int
    sampled: int
    executed: bool
    surprisal_sum: float
    action_tokens: int


def percentile(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def checkpoint_metrics(rows: list[Outcome]) -> dict[str, object]:
    successes = sum(r["success"] for r in rows)
    tokens = sum(r["tokens"] for r in rows)
    calls = sum(r["sampled"] for r in rows)
    action = sum(r["action_tokens"] for r in rows)
    return {
        "declared": DECLARED_PER_POINT,
        "completed": len(rows),
        "successes": successes,
        "success_rate_checked": successes / len(rows) if rows else None,
        "evidence_coverage": len(rows) / DECLARED_PER_POINT,
        "total_tokens_known": tokens,
        "tokens_per_success": tokens / successes if successes else None,
        "mean_tool_calls": sum(r["tools"] for r in rows) / len(rows) if rows else None,
        "functional_tests_executed": sum(r["executed"] for r in rows),
        "format_rejections": sum(r["format_rejections"] for r in rows),
        "format_rejection_rate": sum(r["format_rejections"] for r in rows) / calls
        if calls
        else None,
        "latency_p50_ms": percentile([r["latency"] for r in rows], 0.5),
        "latency_p95_ms": percentile([r["latency"] for r in rows], 0.95),
        "sampled_surprisal": sum(r["surprisal_sum"] for r in rows) / action if action else None,
        "full_policy_entropy": None,
    }


def bootstrap_delta(groups: dict[str, list[int]]) -> list[float]:
    """Three family clusters, fixed seed, nearest-rank exploratory interval."""
    rng = random.Random(20261108)
    names = sorted(groups)
    values = []
    for _ in range(REPEATS):
        selected = rng.choices(names, k=len(names))
        flattened = [x for name in selected for x in groups[name]]
        values.append(sum(flattened) / len(flattened))
    return [sorted(values)[math.ceil(REPEATS * q) - 1] for q in (0.025, 0.975)]


def analyze_curve(  # noqa: PLR0912, PLR0915 - full evidence audit before reporting
    root: Path, output: Path
) -> dict[str, object]:
    plan = CurvePlan.model_validate_json((root / "plan.json").read_bytes())
    state = CurveState.model_validate_json((root / "curve-state.json").read_bytes())
    if state.plan_sha256 != digest(root / "plan.json") or state.kind != "provider":
        raise ValueError("real provider plan binding required")
    verify_hashes(root, state.artifacts)
    cases = {c.task.case_id: c for c in curve_cases_for(plan.dataset_version)}
    points: dict[int, list[Outcome]] = {slot: [] for slot in CHECKPOINTS}
    paired: dict[int, dict[tuple[str, int], bool]] = {slot: {} for slot in CHECKPOINTS}
    records: list[dict[str, object]] = []
    tokens_total = 0
    unknown_calls = 0
    new_provider_calls = 0
    uncommitted_tokens = 0
    uncommitted_calls = 0
    for row in plan.rows:
        if row.run_id not in state.completed:
            folder = root / "rollouts" / row.run_id
            charges = [
                json.loads(path.read_text())
                for path in (folder / "financial-calls").glob("turn_*.json")
            ]
            known = sum(charge["input_tokens"] + charge["output_tokens"] for charge in charges)
            started = len(list((folder / "sample-started").glob("turn_*.json")))
            uncommitted_tokens += known
            uncommitted_calls += len(charges)
            unknown_calls += max(0, started - len(charges))
            records.append(
                {
                    "run_id": row.run_id,
                    "phase": row.phase,
                    "status": "uncommitted" if folder.exists() else "not_run",
                    "known_tokens": known,
                    "known_calls": len(charges),
                    "unknown_calls": max(0, started - len(charges)),
                }
            )
            continue
        before = state.initial if row.phase == "dev" else state.snapshots.get(row.checkpoint)
        if row.phase == "train":
            group_path = root / "updates" / row.group / "group.json"
            before = (
                GroupRecord.model_validate_json(group_path.read_bytes()).before
                if group_path.exists()
                else state.current
            )
        if before is None:
            raise ValueError("completed row has no policy binding")
        folder = root / "rollouts" / row.run_id
        value = load_rollout(cases[row.case_id], folder, before.policy)
        if value.run_id != row.run_id or value.seed != row.seed:
            raise ValueError("analysis queue identity mismatch")
        raw = json.loads((folder / "result.json").read_text())
        trace = TraceRun.from_file(folder / "trace.jsonl", run_id=row.run_id)
        formats = len(trace.by_name("model_output_rejected"))
        calls = len(value.turns)
        finance = list((folder / "financial-calls").glob("turn_*.json"))
        if len(finance) != calls:
            raise ValueError("financial sampling evidence incomplete")
        for item, turn in zip(sorted(finance), value.turns, strict=True):
            charge = json.loads(item.read_text())
            if charge["input_tokens"] != len(turn.prompt) or charge["output_tokens"] != len(
                turn.completion
            ):
                raise ValueError("financial token mismatch")
        outcome: Outcome = {
            "success": value.success,
            "tokens": value.total_tokens,
            "tools": raw["tool_calls"],
            "latency": raw["duration_ms"],
            "format_rejections": formats,
            "sampled": calls,
            "executed": value.verification_executed,
            "surprisal_sum": -sum(sum(t.logprobs) for t in value.turns),
            "action_tokens": sum(len(t.completion) for t in value.turns),
        }
        tokens_total += value.total_tokens
        new_provider_calls += calls
        unknown_calls += len(list((folder / "sample-started").glob("turn_*.json"))) - calls
        if row.phase == "validation":
            points[row.checkpoint].append(outcome)
            paired[row.checkpoint][(row.case_id, row.seed)] = value.success
        records.append(
            {
                "run_id": row.run_id,
                "case_id": row.case_id,
                "phase": row.phase,
                "checkpoint": row.checkpoint,
                "status": "completed",
                "loop_status": value.status,
                "budget_violations": value.budget_violations,
                **outcome,
            }
        )
    delta = None
    interval = None
    family_deltas = {}
    if len(paired[0]) == DECLARED_PER_POINT and set(paired[0]) == set(paired[24]):
        groups: dict[str, list[int]] = {}
        for key, success in paired[0].items():
            groups.setdefault(cases[key[0]].family_group, []).append(
                int(paired[24][key]) - int(success)
            )
        delta = sum(sum(xs) for xs in groups.values()) / DECLARED_PER_POINT
        family_deltas = {name: sum(xs) / len(xs) for name, xs in groups.items()}
        interval = bootstrap_delta(groups)
    finance_dir = (
        root / "finance" if plan.finance_directory is None else Path(plan.finance_directory)
    )
    migration = Migration.model_validate_json((finance_dir / "migration.json").read_bytes())
    ledger_path = (
        root / "budget-after.json"
        if (root / "budget-after.json").exists()
        else finance_dir / "budget.json"
    )
    ledger = _State.model_validate_json(ledger_path.read_bytes())
    account = _restore(ledger)
    pending = [key for key, charge in ledger.charges.items() if charge.status == "pending"]
    held: list[str] = []
    if plan.recovery_sha256 is not None:
        proof_path = root / "recovery-evidence.json"
        if digest(proof_path) != plan.recovery_sha256:
            raise ValueError("recovery report binding changed")
        proof = validate_failure(proof_path)
        held = [key for key in (*proof.prior_holds, proof.operation) if key in pending]
    effective_pending = [key for key in pending if key not in held]
    readiness_path = root / "readiness.json"
    clipped = [
        json.loads(p.read_text())["clipped_share"]
        for p in sorted((root / "updates").glob("*/policy_*.json"))
    ]
    passed = (
        state.phase == "finished"
        and not effective_pending
        and delta is not None
        and delta > 0
        and sum(value > 0 for value in family_deltas.values()) >= MIN_GROUPS
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "phase": state.phase,
        "dataset_version": plan.dataset_version,
        "pending_operation": state.pending,
        "readiness_reused": plan.readiness_sha256 is not None,
        "declared_trajectories": len(plan.rows),
        "completed_trajectories": len(state.completed),
        "new_provider_calls": new_provider_calls + uncommitted_calls,
        "unknown_provider_calls": unknown_calls,
        "known_rollout_tokens": tokens_total + uncommitted_tokens,
        "uncommitted_known_tokens": uncommitted_tokens,
        "uncommitted_known_calls": uncommitted_calls,
        "train_groups": len(state.groups),
        "optimizer_updates": len(state.updated),
        "effective_update_rate": len(state.updated) / len(state.groups) if state.groups else None,
        "dev": json.loads(readiness_path.read_text()) if readiness_path.exists() else None,
        "checkpoints": {str(k): checkpoint_metrics(rows) for k, rows in points.items()},
        "paired_success_delta": delta,
        "family_delta": family_deltas,
        "exploratory_ci95": interval,
        "learning_gate_passed": passed,
        "max_fresh_policy_clipped_share": max(clipped, default=None),
        "stage_committed_cny": str(
            account.committed
            - (
                plan.budget_baseline
                if plan.budget_baseline is not None
                else migration.baseline_committed
            )
        ),
        "cumulative_committed_cny": str(account.committed),
        "pending_operations": pending,
        "held_prior_initialization_operations": held,
        "effective_pending_operations": effective_pending,
        "source_plan_sha256": digest(root / "plan.json"),
        "state_sha256": digest(root / "curve-state.json"),
        "generator_sha256": digest(Path(__file__)),
        "trajectories": records,
        "limitations": [
            "One training seed and three previously seen algorithm families.",
            "Validation instances share specifications, reference code and defect primitives.",
            "Sampled surprisal is not full policy entropy; that entropy is unmeasured.",
            "Money fields are conservative occupied estimates, not settled provider bills.",
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
    analyze_curve(args.directory, args.output)


if __name__ == "__main__":
    main()

"""Offline Base/GSPO efficacy and raw-update audit; no provider calls or usage export."""

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import torch

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.analyze_v2 import turns
from minidsh_rl.contracts import advantages
from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.curve_execution import CurveState, accept_update
from minidsh_rl.gspo import GSPOBatch, GSPOConfig, build_gspo_batch, make_loss
from minidsh_rl.gspo_study import CONFIG, GROUPS, StudyPlan, StudyRow, batch_digest
from minidsh_rl.gspo_study_run import load_study
from minidsh_rl.gspo_trainer import MAX_FRESH_CLIPPED
from minidsh_rl.learning_execution import GroupRecord
from minidsh_rl.learning_schedule import digest
from minidsh_rl.report import paired_interval
from minidsh_rl.tasks_v2 import expansion_tasks
from minidsh_rl.weight_probe import weight_delta


def audit_loss(folder: Path, slot: int, batch: GSPOBatch) -> None:
    values = [
        torch.tensor(row, dtype=torch.float32, requires_grad=True)
        for row in json.loads((folder / f"current_logprobs_{slot:03}.json").read_text())
    ]
    loss, measured = make_loss(batch, CONFIG)(list(batch.datums), values)
    _, paper = make_loss(batch, GSPOConfig())(list(batch.datums), values)
    measured["gspo/paper_clip_fraction"] = paper["gspo/active_clip_fraction"]
    saved = json.loads((folder / f"custom_metrics_{slot:03}.json").read_text())
    if saved.keys() != measured.keys() or any(
        not math.isclose(saved[k], value, rel_tol=1e-6, abs_tol=1e-7)
        for k, value in measured.items()
    ):
        raise ValueError("GSPO objective metrics differ from raw logprobs")
    if measured["gspo/active_clip_fraction"] > MAX_FRESH_CLIPPED:
        raise ValueError("fresh-policy clipping guard failed")
    gradients = torch.autograd.grad(loss, values)
    for meta in batch.trajectories:
        for index, old in zip(meta.indices, meta.sampling, strict=True):
            gradient = gradients[index]
            if not bool(torch.isfinite(gradient).all()):
                raise ValueError("nonfinite GSPO gradient")
            if bool((gradient[: -len(old)] != 0).any()):
                raise ValueError("masked prompt/observation received gradient")


def checked_result(root: Path, row: StudyRow, policy_id: str) -> dict[str, object]:
    folder = root / "rollouts" / row.run_id
    result: dict[str, object] = json.loads((folder / "result.json").read_text())
    if any(result[k] != getattr(row, k) for k in ("run_id", "case_id", "seed")) or (
        result["policy_id"] != policy_id
        or result["criterion_version"] != "expansion_v2"
        or not isinstance(result["success"], bool)
    ):
        raise ValueError("raw rollout identity mismatch")
    if result["candidate_hash"]:
        verdict = json.loads((folder / "verification/result.json").read_text())
        if (
            digest(folder / "candidate.py") != result["candidate_hash"]
            or verdict["case_id"] != row.case_id
            or verdict["passed"] != result["success"]
            or verdict["criterion_version"] != "expansion_v2"
        ):
            raise ValueError("independent verification mismatch")
    elif result["success"] is not False:
        raise ValueError("success requires independent candidate verification")
    trace = TraceRun.from_file(folder / "trace.jsonl", run_id=row.run_id)
    raw = turns(folder)
    if (
        not raw
        or len(raw) != len(trace.by_name("model_call_completed"))
        or trace.final() is None
        or trace.status() != result["status"]
    ):
        raise ValueError("raw trajectory/Trace coverage incomplete")
    return result


def audit_training(root: Path, plan: StudyPlan, state: CurveState) -> dict[str, object]:
    replay = CurveState(plan_sha256=state.plan_sha256, kind="provider", current=state.initial)
    successes = 0
    for slot in range(GROUPS):
        key = f"train_{slot:03}"
        folder = root / "updates" / key
        record = GroupRecord.model_validate_json((folder / "group.json").read_bytes())
        rows = [r for r in plan.rows if r.phase == "train" and r.slot == slot]
        if tuple(r.run_id for r in rows) != record.run_ids:
            raise ValueError("training group queue differs")
        values = [checked_result(root, row, record.before.policy.policy_id) for row in rows]
        rewards = tuple(float(v["success"] is True) for v in values)
        batch = build_gspo_batch(
            tuple(turns(root / "rollouts" / row.run_id) for row in rows), advantages(rewards)
        )
        saved = json.loads((folder / "rewards.json").read_text())
        if (
            saved["rewards"] != list(rewards)
            or saved["advantages"] != list(advantages(rewards))
            or record.datum_sha256 != batch_digest(batch)
            or record.has_signal != batch.has_signal
        ):
            raise ValueError("raw reward, advantage, mask or datum mismatch")
        accept_update(root, replay, key)
        if batch.has_signal:
            audit_loss(folder, slot, batch)
        successes += sum(v["success"] is True for v in values)
    if (
        replay.groups != state.groups
        or replay.updated != state.updated
        or replay.current != state.current
    ):
        raise ValueError("receipt chain differs from final controller state")
    return {
        "groups": GROUPS,
        "trajectories": GROUPS * 4,
        "actual_updates": len(replay.updated),
        "zero_signal_groups": GROUPS - len(replay.updated),
        "training_successes": successes,
        "reconstructed_datum_coverage": 1.0,
        "loss_and_mask_audit": "passed",
    }


def evaluation(root: Path, plan: StudyPlan, state: CurveState, phase: str) -> dict[str, object]:
    if state.initial is None or state.current is None:
        raise ValueError("paired checkpoints missing")
    rows = [r for r in plan.rows if r.phase == phase]
    families = {task.case_id: task.family for task in expansion_tasks()}
    results = {}
    counts: Counter[str] = Counter()
    failures: dict[str, Counter[str]] = {"base": Counter(), "gspo": Counter()}
    for row in rows:
        policy = state.initial if row.policy == "base" else state.current
        value = checked_result(root, row, policy.policy.policy_id)
        results[row.run_id] = value["success"] is True
        counts[row.policy] += value["success"] is True
        if value["success"] is False:
            failures[row.policy][str(value["failure_type"])] += 1
    pairs = []
    wins = losses = 0
    for first, second in zip(rows[::2], rows[1::2], strict=True):
        if first.case_id != second.case_id or first.seed != second.seed:
            raise ValueError("unpaired evaluation")
        pair = {r.policy: results[r.run_id] for r in (first, second)}
        pairs.append((families[first.case_id], pair["base"], pair["gspo"]))
        wins += pair["gspo"] and not pair["base"]
        losses += pair["base"] and not pair["gspo"]
    interval = paired_interval(pairs)
    interval["limitation"] = "single training seed; family-cluster exploratory interval"
    denominator = len(rows) // 2
    return {
        "declared_trajectories": len(rows),
        "completed_trajectories": len(rows),
        "base": {
            "successes": counts["base"],
            "denominator": denominator,
            "success_rate": counts["base"] / denominator,
        },
        "gspo": {
            "successes": counts["gspo"],
            "denominator": denominator,
            "success_rate": counts["gspo"] / denominator,
        },
        "gspo_only_success": wins,
        "base_only_success": losses,
        "paired_difference": interval,
        "failure_types": {k: dict(v) for k, v in failures.items()},
    }


def analyze(root: Path) -> dict[str, object]:
    plan = load_study(root)
    state = CurveState.model_validate_json((root / "state.json").read_bytes())
    verify_hashes(root, state.artifacts)
    if (
        state.kind != "provider"
        or state.phase != "finished"
        or state.pending is not None
        or state.plan_sha256 != digest(root / "plan.json")
        or state.completed != [row.run_id for row in plan.rows]
    ):
        raise ValueError("entire frozen provider queue required; never drop missing rows")
    training = audit_training(root, plan, state)
    initial = root / "initial-sdk/initial.archive"
    final = root / "final-audit/final.archive"
    initial_proof = json.loads((root / "initial-sdk/initial-equivalence.json").read_text())
    final_proof = json.loads((root / "final-audit/result.json").read_text())
    reference = json.loads((root / "reference.json").read_text())
    initial_delta = weight_delta(Path(reference["archive"]), initial)
    delta = weight_delta(initial, final)
    if (
        initial_delta != 0
        or initial_proof["adapter_delta_l2"] != 0
        or delta <= 0
        or not math.isclose(delta, final_proof["parameter_delta_l2"], rel_tol=1e-10, abs_tol=1e-12)
        or final_proof["initial_sha256"] != digest(initial)
        or final_proof["final_sha256"] != digest(final)
    ):
        raise ValueError("actual initial/final parameter evidence differs")
    return {
        "version": plan.version,
        "plan_sha256": digest(root / "plan.json"),
        "declared_trajectories": len(plan.rows),
        "completed_trajectories": len(state.completed),
        "training": training,
        "validation": evaluation(root, plan, state, "validation"),
        "test": evaluation(root, plan, state, "test"),
        "weights": {"initial_delta_from_grpo_seed17": initial_delta, "trained_delta_l2": delta},
        "algorithm": "gspo_multiturn_wide_clip_v1",
        "clip_low": CONFIG.clip_low,
        "clip_high": CONFIG.clip_high,
        "comparison": plan.comparison,
        "limitations": [
            "previously opened fixed benchmark; not a new blind test",
            "single training seed; no GRPO-vs-GSPO efficacy claim",
        ],
        "evidence": {
            "root": str(root),
            "state_sha256": digest(root / "state.json"),
            "source_archive_sha256": digest(root / "source.tar.gz"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = analyze(args.directory)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    sys.stdout.write(
        json.dumps({"audited": True, "completed": value["completed_trajectories"]}) + "\n"
    )


if __name__ == "__main__":
    main()

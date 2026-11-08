"""Audit v2 training/evaluation artifacts and apply the predeclared extension rule."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from minidsh.evaluation.harness_metrics import ScoringBudget, ScoringEvidence, score_harness
from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.analyze_run import repeated_cat_reads, trace_metrics
from minidsh_rl.contracts import Turn, advantages
from minidsh_rl.datum import build_batch
from minidsh_rl.evaluate_run import verify_sources
from minidsh_rl.evaluate_v2 import evaluation_queue
from minidsh_rl.report import paired_interval, summarize
from minidsh_rl.reprice import milli_upper
from minidsh_rl.train_v2 import PILOT_SLOTS
from minidsh_rl.trainer import MAX_CLIPPED_SHARE, normalized_batch

MIN_EFFECTIVE = 8
EXTENSION_EFFECTIVE = 12


def turns(directory: Path) -> tuple[Turn, ...]:
    """Original sampled Token IDs/logprobs are the training evidence."""
    result = []
    for path in sorted((directory / "tokens").glob("turn_*.json")):
        raw = json.loads(path.read_text())
        result.append(
            Turn(
                tuple(raw["prompt_token_ids"]),
                tuple(raw["completion_token_ids"]),
                tuple(raw["old_logprobs"]),
            )
        )
    return tuple(result)


def audit_training(directory: Path) -> dict[str, object]:
    """Rebuild masks, advantage and normalized Datum hash for every declared group."""
    verify_sources(directory)
    queue = json.loads((directory / "queue.json").read_text())
    effective = raw_tokens = 0
    for row in queue:
        slot = row["slot"]
        group = json.loads((directory / f"group_{slot:03}.json").read_text())
        samples = [directory / name for name in group["run_ids"]]
        results = [json.loads((sample / "result.json").read_text()) for sample in samples]
        reward = tuple(float(r["success"]) for r in results)
        if list(reward) != group["rewards"] or list(advantages(reward)) != group["advantages"]:
            raise ValueError("reward/group advantage mismatch")
        batch = normalized_batch(
            build_batch(tuple(turns(s) for s in samples), advantages(reward)), "sample_mean"
        )
        serialized = json.dumps([d.model_dump(mode="json") for d in batch.datums], sort_keys=True)
        journal = json.loads((directory / f"update_{slot:03}.json").read_text())
        if journal["datum_hash"] != hashlib.sha256(serialized.encode()).hexdigest():
            raise ValueError("reconstructed Datum/mask hash differs")
        if row["updated"] is not batch.has_signal:
            raise ValueError("zero/nonzero group update mismatch")
        if journal["state"] not in (
            {"optim_confirmed", "checkpointed"} if batch.has_signal else {"skipped"}
        ):
            raise ValueError("unknown or unconfirmed update")
        if batch.has_signal:
            diagnostic = json.loads((directory / f"policy_{slot:03}.json").read_text())
            if diagnostic["clipped_share"] > MAX_CLIPPED_SHARE:
                raise ValueError("training policy numerical guard failed")
        effective += batch.has_signal
        raw_tokens += sum(r["total_tokens"] for r in results)
    return {
        "declared_slots": len(queue),
        "audited_slots": len(queue),
        "nonzero_updates": effective,
        "total_rollouts": len(queue) * 4,
        "sampling_tokens": raw_tokens,
        "datum_hash_coverage": 1.0,
    }


def extension_rule(
    base: dict[str, object], rl: dict[str, object], *, effective: int, end_slot: int
) -> dict[str, object]:
    """A resource-allocation rule, not a statistical claim of learning benefit."""
    b = int(str(base["successes"]))
    r = int(str(rl["successes"]))
    b_eff = base["tokens_per_success"]
    r_eff = rl["tokens_per_success"]
    direction = r >= b + 2 or (
        r >= b
        and b_eff is not None
        and r_eff is not None
        and float(str(r_eff)) <= 0.9 * float(str(b_eff))
    )
    allowed = effective >= MIN_EFFECTIVE and (
        direction or (effective >= EXTENSION_EFFECTIVE and end_slot == PILOT_SLOTS)
    )
    return {
        "directional_signal": direction,
        "allow_extension": allowed,
        "allowed_end_slot": 120 if allowed and direction else 60 if allowed else None,
        "reason": "directional_resource_gate"
        if allowed and direction
        else "single_predeclared_60_slot_probe"
        if allowed
        else "stop_no_direction_or_signal",
        "allow_final_test": False,
    }


def analyze(evaluation: Path, training: Path) -> dict[str, object]:  # noqa: PLR0912
    training_audit = audit_training(training)
    complete = json.loads((evaluation / "summary.json").read_text())
    split = complete["split"]
    expected = evaluation_queue(split)
    queue = json.loads((evaluation / "queue.json").read_text())
    if complete["completed"] != len(expected) or len(queue) != len(expected):
        raise ValueError("full paired cohort is required")
    rows = []
    scores = {}
    for row, original in zip(queue, expected, strict=True):
        if any(
            row[k] != original[k]
            for k in ("case_id", "family", "split", "repeat", "seed", "policy")
        ):
            raise ValueError("paired queue changed")
        run = evaluation / row["run_id"]
        result = json.loads((run / "result.json").read_text())
        if any(row[k] != result[k] for k in ("case_id", "seed", "success", "status", "tool_calls")):
            raise ValueError("queue/result mismatch")
        if result["policy_id"] != row["policy"]:
            raise ValueError("wrong policy")
        if result["candidate_hash"]:
            if (
                hashlib.sha256((run / "candidate.py").read_bytes()).hexdigest()
                != result["candidate_hash"]
            ):
                raise ValueError("candidate hash differs")
            verdict = json.loads((run / "verification/result.json").read_text())
            if (
                verdict["case_id"] != row["case_id"]
                or verdict["passed"] != row["success"]
                or verdict["criterion_version"] != "expansion_v2"
            ):
                raise ValueError("independent verdict differs")
        elif row["success"] is not False:
            raise ValueError("success without candidate")
        trace = TraceRun.from_file(run / "trace.jsonl", run_id=row["run_id"])
        raw = turns(run)
        inputs, outputs = sum(len(t.prompt) for t in raw), sum(len(t.completion) for t in raw)
        if (
            (inputs, outputs) != trace.total_tokens()[:2]
            or inputs + outputs != row["tokens"]
            or trace.status() != row["status"]
        ):
            raise ValueError("Trace/raw usage mismatch")
        if len(raw) != len(trace.by_name("model_call_completed")) or trace.final() is None:
            raise ValueError("incomplete raw/Trace coverage")
        score = score_harness(
            trace,
            evidence=ScoringEvidence(
                run_id=row["run_id"],
                case_id=row["case_id"],
                source=str(run / "result.json"),
                task_success=row["success"],
                budget=ScoringBudget(
                    max_steps=8, max_tokens=24000, max_tool_calls=8, max_duration_seconds=120
                ),
            ),
        )
        scores[row["run_id"]] = score
        cost = sum((milli_upper(len(t.prompt), len(t.completion)) for t in raw), Decimal(0))
        rows.append(
            dict(row, sample_upper_cny=str(cost), repeated_cat_read_proxy=repeated_cat_reads(trace))
        )
    sections = {}
    declared = len(expected) // 2
    for policy in ("base", "rl"):
        subset = [r for r in rows if r["policy"] == policy]
        summary = summarize(subset, declared=declared)
        cost = sum((Decimal(r["sample_upper_cny"]) for r in subset), Decimal(0))
        success = int(str(summary["successes"]))
        summary.update(
            sample_upper_cny=str(cost),
            sample_upper_per_success_cny=str(cost / success) if success else None,
            trace_metrics=trace_metrics([scores[r["run_id"]] for r in subset]),
            failure_types=dict(Counter(r["failure_type"] for r in subset if not r["success"])),
        )
        sections[policy] = summary
    pairs = []
    for first, second in zip(rows[::2], rows[1::2], strict=True):
        policies = {r["policy"]: r for r in (first, second)}
        pairs.append(
            (
                str(first["family"]),
                policies["base"]["success"] is True,
                policies["rl"]["success"] is True,
            )
        )
    parent = json.loads((training / "summary.json").read_text())
    decision = (
        extension_rule(
            sections["base"],
            sections["rl"],
            effective=int(str(training_audit["nonzero_updates"])),
            end_slot=parent["completed_until_slot"],
        )
        if split == "validation"
        else {}
    )
    return {
        "schema_version": 1,
        "split": split,
        "declared": len(expected),
        "completed": len(rows),
        "training_audit": training_audit,
        "policies": sections,
        "paired": paired_interval(pairs),
        "discordance": {
            "base_only": sum(b and not r for _, b, r in pairs),
            "rl_only": sum(r and not b for _, b, r in pairs),
        },
        "parent_summary_sha256": hashlib.sha256(
            (training / "summary.json").read_bytes()
        ).hexdigest(),
        **decision,
        "runs": rows,
        "limitations": [
            "synthetic function bugs, single training seed; no real issue generalization",
            "validation viewed during staged resource decisions; test remains separate",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.evaluation, args.training)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()

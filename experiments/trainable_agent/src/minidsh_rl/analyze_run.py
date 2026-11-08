"""Read-only audit and reproducible statistics for the frozen core evaluation."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import cast

from minidsh.evaluation.harness_metrics import (
    HarnessMetrics,
    ScoringBudget,
    ScoringEvidence,
    aggregate_harness_metrics,
    score_harness,
)
from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.budget import cost_upper_bound
from minidsh_rl.contracts import Turn
from minidsh_rl.evaluate_run import evaluation_queue, verify_sources
from minidsh_rl.report import paired_interval, summarize


def read(path: Path) -> dict[str, object]:
    """Load an object, rejecting malformed artifact boundaries."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: " + str(path))
    return cast(dict[str, object], value)


def trace_metrics(scores: list[HarnessMetrics]) -> dict[str, object]:
    """SDK does not expose cached usage; adapter placeholder zero is not evidence."""
    result = cast(dict[str, object], aggregate_harness_metrics(scores))
    measurements = cast(dict[str, object], result["measurements"])
    measurements["cached_input_tokens_raw_placeholder"] = measurements["cached_input_tokens"]
    measurements["cached_input_tokens"] = None
    rates = cast(dict[str, object], result["rates"])
    for name, eligible in (
        ("cache_hit_rate", measurements["model_calls"]),
        ("cached_input_share", measurements["input_tokens"]),
    ):
        rates[name] = {
            "value": None,
            "numerator": 0,
            "denominator": 0,
            "eligible": eligible,
            "coverage": 0.0 if eligible else None,
        }
    return result


def repeated_cat_reads(trace: TraceRun) -> dict[str, int]:
    """Identical successful cat output is a repetition proxy, not a waste label."""
    seen: set[str] = set()
    repeated = eligible = 0
    for record in trace.by_name("tool_call_completed"):
        result = record.data.get("result")
        if not isinstance(result, dict):
            continue
        argv = result.get("argv")
        if not isinstance(argv, list) or not argv or argv[0] != "cat":
            continue
        if result.get("exit_code") != 0 or record.data.get("truncated") is not False:
            continue
        eligible += 1
        identity = json.dumps([argv, result.get("stdout"), result.get("stderr")], sort_keys=True)
        repeated += identity in seen
        seen.add(identity)
    return {"numerator": repeated, "denominator": eligible, "eligible": eligible}


def audit_run(directory: Path, row: dict[str, object]) -> tuple[dict[str, object], HarnessMetrics]:
    """Bind immutable candidate, independent verdict, exact tokens and Trace to one run."""
    result = read(directory / "result.json")
    for key in (
        "run_id",
        "case_id",
        "seed",
        "split",
        "success",
        "status",
        "tool_calls",
        "duration_ms",
    ):
        if result[key] != row[key]:
            raise ValueError("queue/result mismatch: " + key)
    if result["policy_id"] != row["policy"] or result["total_tokens"] != row["tokens"]:
        raise ValueError("queue policy/token mismatch")
    if result["candidate_hash"] is not None:
        if (
            hashlib.sha256((directory / "candidate.py").read_bytes()).hexdigest()
            != result["candidate_hash"]
        ):
            raise ValueError("candidate hash mismatch")
        verdict = read(directory / "verification/result.json")
        if verdict["case_id"] != row["case_id"] or verdict["passed"] != result["success"]:
            raise ValueError("independent verdict mismatch")
        if verdict["criterion_version"] != "lightweight_v2":
            raise ValueError("unfrozen verifier")
    elif result["success"] is not False:
        raise ValueError("success without a captured candidate")
    trace = TraceRun.from_file(directory / "trace.jsonl", run_id=str(row["run_id"]))
    if trace.final() is None or trace.status() != row["status"]:
        raise ValueError("nonterminal or mismatched Trace")
    raw_calls = []
    for path in sorted((directory / "tokens").glob("turn_*.json")):
        raw = read(path)
        turn = Turn(
            tuple(cast(list[int], raw["prompt_token_ids"])),
            tuple(cast(list[int], raw["completion_token_ids"])),
            tuple(cast(list[float], raw["old_logprobs"])),
        )
        if raw["run_id"] != row["run_id"] or raw["policy_id"] != row["policy"]:
            raise ValueError("token policy/run mismatch")
        raw_calls.append(turn)
    inputs = sum(len(turn.prompt) for turn in raw_calls)
    outputs = sum(len(turn.completion) for turn in raw_calls)
    trace_inputs, trace_outputs, _ = trace.total_tokens()
    if (inputs, outputs) != (trace_inputs, trace_outputs) or inputs + outputs != row["tokens"]:
        raise ValueError("raw/Trace/queue token reconciliation failed")
    evidence = ScoringEvidence(
        run_id=str(row["run_id"]),
        case_id=str(row["case_id"]),
        source=str(directory / "result.json"),
        task_success=cast(bool, result["success"]),
        budget=ScoringBudget(
            max_steps=8, max_tokens=24000, max_tool_calls=8, max_duration_seconds=120.0
        ),
    )
    score = score_harness(trace, evidence=evidence)
    final = trace.final()
    assert final is not None
    detail = dict(row)
    detail.update(
        {
            "input_tokens": inputs,
            "output_tokens": outputs,
            "model_rpc_records": len(raw_calls),
            "malformed_responses": len(trace.by_name("model_output_rejected")),
            "unknown_usage_calls_raw": score.measurements["unknown_usage_calls"],
            "model_failed_events": [record.data for record in trace.by_name("model_call_failed")],
            "loop_latency": final.data.get("latency"),
            "verification": read(directory / "verification/result.json")
            if result["candidate_hash"]
            else None,
            "candidate_hash": result["candidate_hash"],
            "repeated_cat_read_proxy": repeated_cat_reads(trace),
            "model_cost_upper_cny": str(
                sum(
                    (cost_upper_bound(len(t.prompt), len(t.completion), 0) for t in raw_calls),
                    Decimal("0"),
                )
            ),
            "trace_path": str(directory / "trace.jsonl"),
            "result_path": str(directory / "result.json"),
        }
    )
    return detail, score


def analyze(evaluation: Path, training: Path, budget: Path) -> dict[str, object]:
    """Require all declared rows; never silently remove unfinished or failed opportunities."""
    verify_sources(training)
    completed = read(evaluation / "completed.json")
    declared_count = len(evaluation_queue())
    if completed["declared"] != declared_count or completed["completed"] != declared_count:
        raise ValueError("evaluation not complete")
    rows = cast(list[dict[str, object]], json.loads((evaluation / "queue.json").read_text()))
    expected = evaluation_queue()
    if len(rows) != len(expected):
        raise ValueError("declared denominator changed")
    details = []
    scores: dict[str, HarnessMetrics] = {}
    for row, original in zip(rows, expected, strict=True):
        if any(
            row[key] != original[key]
            for key in ("case_id", "family", "split", "repeat", "seed", "policy")
        ):
            raise ValueError("frozen pair/seed/order changed")
        detail, score = audit_run(evaluation / str(row["run_id"]), row)
        details.append(detail)
        scores[str(row["run_id"])] = score
    sections = {}
    for split, declared in (("validation", 8), ("test", 32)):
        section: dict[str, object] = {}
        for policy in ("base", "rl"):
            subset = [r for r in details if r["split"] == split and r["policy"] == policy]
            summary = summarize(subset, declared=declared)
            cost = sum((Decimal(str(r["model_cost_upper_cny"])) for r in subset), Decimal("0"))
            successes = int(str(summary["successes"]))
            summary.update(
                {
                    "model_cost_upper_cny": str(cost),
                    "model_cost_upper_per_success_cny": str(cost / successes)
                    if successes
                    else None,
                    "malformed_responses": sum(int(str(r["malformed_responses"])) for r in subset),
                    "model_rpc_records": sum(int(str(r["model_rpc_records"])) for r in subset),
                    "failure_types": dict(
                        Counter(str(r["failure_type"]) for r in subset if r["success"] is False)
                    ),
                    "terminal_statuses": dict(Counter(str(r["status"]) for r in subset)),
                    "trace_metrics": trace_metrics([scores[str(r["run_id"])] for r in subset]),
                }
            )
            repeat_counts = [cast(dict[str, int], r["repeated_cat_read_proxy"]) for r in subset]
            numerator = sum(r["numerator"] for r in repeat_counts)
            denominator = sum(r["denominator"] for r in repeat_counts)
            summary["repeated_cat_read_proxy"] = {
                "numerator": numerator,
                "denominator": denominator,
                "eligible": denominator,
                "value": numerator / denominator if denominator else None,
                "coverage": 1.0 if denominator else None,
                "scope": "successful untruncated cat calls only; Python reads unchecked; "
                "legitimate rereads may count",
            }
            section[policy] = summary
        pairs = []
        for left, right in zip(details[::2], details[1::2], strict=True):
            if left["split"] != split:
                continue
            policies = {str(r["policy"]): r for r in (left, right)}
            pairs.append(
                (
                    str(left["family"]),
                    policies["base"]["success"] is True,
                    policies["rl"]["success"] is True,
                )
            )
        section["paired"] = paired_interval(pairs)
        section["discordance"] = {
            "base_only": sum(b and not r for _, b, r in pairs),
            "rl_only": sum(r and not b for _, b, r in pairs),
        }
        sections[split] = section
    ledger = read(budget)
    if ledger["pending"] or Decimal(str(ledger["spent"])) > Decimal("50"):
        raise ValueError("pending provider cost or exceeded budget")
    train_summary = read(training / "summary.json")
    eval_cost = sum((Decimal(str(r["model_cost_upper_cny"])) for r in details), Decimal("0"))
    if (
        Decimal(str(ledger["spent"])) - Decimal(str(train_summary["conservative_spend_cny"]))
        != eval_cost
    ):
        raise ValueError("evaluation cost does not reconcile with the shared ledger")
    paths = [
        evaluation / "queue.json",
        evaluation / "completed.json",
        training / "summary.json",
        budget,
    ]
    paths.extend(
        p for p in evaluation.rglob("*") if p.is_file() and p.suffix in {".json", ".jsonl", ".py"}
    )
    paths.extend(
        Path(p)
        for p in (
            "experiments/trainable_agent/src/minidsh_rl/analyze_run.py",
            "experiments/trainable_agent/src/minidsh_rl/report.py",
            "src/minidsh/evaluation/harness_metrics.py",
        )
    )
    return {
        "schema_version": 1,
        "training": train_summary,
        "evaluation": sections,
        "runs": details,
        "budget": {
            "limit_cny": "50",
            "conservative_spend_cny": ledger["spent"],
            "pending": ledger["pending"],
            "evaluation_cny": str(eval_cost),
            "s0_cny": "6.09",
            "core_training_cny": str(
                Decimal(str(train_summary["conservative_spend_cny"])) - Decimal("6.09")
            ),
        },
        "provenance": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))
        },
        "limitations": [
            "one training seed; four held-out test families; two inference repeats are correlated",
            "cache telemetry unavailable; raw unknown usage retained; "
            "do not infer zero cached usage",
            "model timing includes adapter overhead; end-to-end includes sandbox/verification",
            "currency is conservative per-RPC rounded accounting, not provider invoice; "
            "storage/unsettled bills require separate audit",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.evaluation, args.training, args.budget)
    if args.output.exists():
        raise ValueError("derived report already exists; choose a new version")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.write(
        json.dumps(
            {"budget": result["budget"], "evaluation": result["evaluation"]}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()

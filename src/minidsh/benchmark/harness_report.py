"""Recompute pilot scores from immutable traces, candidates, and verifier receipts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from minidsh.benchmark.harness_study import (
    COMPACTION_BUDGET,
    CRITICAL,
    INPUT_ADMISSION,
    MAX_DURATION_MS,
    MAX_STEPS,
    MAX_TOOLS,
    RECOVERY_PREFIX,
    StudyProtocol,
)
from minidsh.benchmark.public_swe import summarize, write_json
from minidsh.domain import JsonValue
from minidsh.evaluation.harness_metrics import ScoringEvidence, score_harness
from minidsh.evaluation.tracefile import TraceRecord, TraceRun


def digest(path: Path) -> str:
    """Content hash used for inputs, artifacts and scorer provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _argv(record: TraceRecord) -> list[str]:
    arguments = record.data.get("arguments")
    raw = arguments.get("argv") if isinstance(arguments, dict) else None
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def _ok(record: TraceRecord) -> bool:
    result = record.data.get("result")
    return (
        record.data.get("status") == "succeeded"
        and isinstance(result, dict)
        and result.get("exit_code") == 0
    )


def trajectory_checks(
    trace: TraceRun, group: str, variant: str, language: str, *, success: bool
) -> dict[str, JsonValue]:
    """Predeclared opportunity checks; recovery needs effective action plus smoke in five calls."""
    prefix = {"context": 0, "output": 1, "recovery": 3}[group]
    contexts = trace.by_name("context_built")
    completed = trace.by_name("tool_call_completed")
    proposed = {str(r.data.get("call_id")): r for r in trace.by_name("tool_call_started")}
    first_api = next((r for r in trace.by_name("model_call_started") if r.step_id > prefix), None)
    first_messages = first_api.data.get("messages", []) if first_api else []
    pinned = isinstance(first_messages, list) and any(
        isinstance(m, dict) and m.get("content") == CRITICAL for m in first_messages
    )
    checks: dict[str, JsonValue] = {
        "peak_estimated_context_tokens": max(
            (int(str(r.data.get("estimated_tokens", 0))) for r in contexts), default=0
        ),
        "omitted_messages": sum(int(str(r.data.get("omitted_messages", 0))) for r in contexts),
        "mechanism_triggered": None,
        "pinned_critical_fact": None,
        "diagnosis_passed": None,
        "recovered_in_window": None,
    }
    if group == "context":
        checks["pinned_critical_fact"] = pinned
        checks["mechanism_triggered"] = (
            checks["omitted_messages"] != 0
            if variant == "B"
            else int(str(checks["peak_estimated_context_tokens"])) > COMPACTION_BUDGET
        )
    elif group == "output":
        log = next((r for r in completed if r.data.get("call_id") == "stimulus_log"), None)
        expected = "regression_head_v1" if language == "python" else "regression_tail_v1"
        final = trace.final()
        # Final answer lives in the authoritative assistant-message commit, not model self-score.
        answers = [r for r in trace.by_name("model_call_completed") if not r.data.get("tool_calls")]
        answer = str(answers[-1].data.get("content", "")) if answers else ""
        checks["diagnosis_passed"] = bool(final and expected in answer)
        checks["diagnostic_retained_in_observation"] = bool(
            log and expected in json.dumps(log.data)
        )
        checks["mechanism_triggered"] = bool(
            log
            and (
                log.data.get("truncated") is True
                if variant == "B"
                else len(json.dumps(log.data)) > INPUT_ADMISSION
            )
        )
    else:
        faults = [
            r for r in completed if str(r.data.get("call_id", "")).startswith("stimulus_fault_")
        ]
        episode = len(faults) == RECOVERY_PREFIX and all(not _ok(r) for r in faults)
        recovery_event = any(
            r.data.get("count") == RECOVERY_PREFIX for r in trace.by_name("loop_guard_triggered")
        )
        checks["mechanism_triggered"] = episode and (recovery_event if variant == "B" else True)
        after = [
            r for r in completed if not str(r.data.get("call_id", "")).startswith("stimulus_")
        ][:5]
        source = "solution.py" if language == "python" else "solution.js"
        smoke = "smoke.py" if language == "python" else "smoke.js"
        effective = False
        smoke_ok = False
        for record in after:
            call = proposed.get(str(record.data.get("call_id")))
            argv = _argv(call) if call else []
            command = " ".join(argv)
            result = record.data.get("result")
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            read = source in command and (
                "def " in str(stdout) or "function " in str(stdout) or "const " in str(stdout)
            )
            patch = source in command and any(
                x in command for x in ("os.replace", "renameSync", ".replace(")
            )
            effective |= _ok(record) and (read or patch)
            smoke_ok |= effective and _ok(record) and smoke in argv
        checks["recovery_opportunity"] = episode
        checks["recovery_hint_emitted"] = recovery_event
        checks["window_tool_calls"] = len(after)
        checks["recovered_in_window"] = episode and effective and smoke_ok and success
    return checks


def score_row(row: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Bind independent candidate-verifier outcome to this run's trace and limits."""
    facts_path = directory / "facts.json"
    facts = json.loads(facts_path.read_text())
    trace = TraceRun.from_file(directory / "trace.jsonl", run_id=facts["run_id"])
    verifier = directory / "verifier" / "result.json"
    success = False
    if verifier.exists():
        receipt = json.loads(verifier.read_text())
        success = receipt["passed"] is True
    candidate_path = directory / facts["candidate_filename"]
    if success and (
        not candidate_path.is_file() or digest(candidate_path) != facts["candidate_sha256"]
    ):
        raise ValueError("candidate changed after verification")
    final = trace.final()
    if final is None:
        raise ValueError("incomplete trace; cannot claim a completed study run")
    inputs, outputs, cached = trace.total_tokens()
    result: dict[str, Any] = dict(
        row,
        run_id=facts["run_id"],
        status=trace.status(),
        success=success,
        tokens=inputs + outputs,
        input_tokens=inputs,
        output_tokens=outputs,
        cached_input_tokens=cached,
        steps=final.data.get("steps"),
        tool_calls=final.data.get("tool_calls"),
        duration_ms=final.data.get("duration_ms"),
        failure_reason=trace.failure_reason(),
        unknown_usage_calls=facts["unknown_usage_calls"],
        api_calls=facts["api_calls"],
        scripted_prefix_calls=facts["scripted_prefix_calls"],
        admission_rejections=facts["admission_rejections"],
        token_overrun=max(0, inputs + outputs - facts["budget"]["max_tokens"]),
        trace_sha256=digest(trace.path),
        facts_sha256=digest(facts_path),
        verifier_sha256=digest(verifier) if verifier.exists() else None,
    )
    result.update(
        trajectory_checks(trace, row["group"], row["variant"], row["language"], success=success)
    )
    goals: dict[str, bool | None] = {"repair": success}
    constraints: dict[str, bool | None] = {"usage_known": facts["unknown_usage_calls"] == 0}
    recovery: dict[str, bool | None] = {}
    if row["group"] == "output":
        goals["diagnosis"] = result["diagnosis_passed"]
        constraints["diagnosis"] = result["diagnosis_passed"]
    if row["group"] == "context":
        constraints["pinned_critical_fact"] = result["pinned_critical_fact"]
    if row["group"] == "recovery":
        recovery["fixed_missing_file_episode"] = result["recovered_in_window"]
    evidence = ScoringEvidence.model_validate(
        {
            "run_id": facts["run_id"],
            "case_id": row["id"],
            "source": "sealed_public_verifier_and_rules_v1",
            "task_success": success,
            "goal_checks": goals,
            "constraint_checks": constraints,
            "recovery_checks": recovery,
            "budget": {
                "max_tokens": facts["budget"]["max_tokens"],
                "max_steps": 16,
                "max_tool_calls": 32,
                "max_duration_seconds": 180.0,
            },
        }
    )
    result["evidence"] = evidence.model_dump(mode="json")
    result["metrics"] = score_harness(trace, evidence=evidence).to_json()
    budget_ok = (
        inputs + outputs <= facts["budget"]["max_tokens"]
        and int(str(result["steps"])) <= MAX_STEPS
        and int(str(result["tool_calls"])) <= MAX_TOOLS
        and float(str(result["duration_ms"])) <= MAX_DURATION_MS
    )
    result["constraint_compliant"] = budget_ok and all(constraints.values())
    result["success_within_constraints"] = success and result["constraint_compliant"]
    return result


def study_report(
    protocol: StudyProtocol, rows: list[dict[str, Any]], fingerprint: str
) -> dict[str, Any]:
    """Show every pair and all failure tokens; avoid inference from four pairs per group."""
    pairs = []
    for pair_id in dict.fromkeys(r["pair_id"] for r in rows):
        pair = {r["variant"]: r for r in rows if r["pair_id"] == pair_id}
        a, b = pair["A"], pair["B"]
        ran = a["status"] != "not_run" and b["status"] != "not_run"
        valid = (
            ran and a.get("mechanism_triggered") is True and b.get("mechanism_triggered") is True
        )
        pairs.append(
            {
                "pair_id": pair_id,
                "valid": valid,
                "A_success": a["success"],
                "B_success": b["success"],
                "input_token_delta_B_minus_A": b.get("input_tokens", 0) - a.get("input_tokens", 0)
                if ran
                else None,
                "total_token_delta_B_minus_A": b["tokens"] - a["tokens"] if ran else None,
            }
        )
    groups: dict[str, Any] = {}
    for group in protocol.groups:
        groups[group] = {}
        for variant in ("A", "B"):
            selected = [r for r in rows if r["group"] == group and r["variant"] == variant]
            summary = summarize(selected, 32000)
            summary.update(
                {
                    "constraint_compliant": sum(
                        r.get("constraint_compliant") is True for r in selected
                    ),
                    "success_within_constraints": sum(
                        r.get("success_within_constraints") is True for r in selected
                    ),
                    "mechanism_triggered": sum(
                        r.get("mechanism_triggered") is True for r in selected
                    ),
                    "recovered_in_window": sum(
                        r.get("recovered_in_window") is True for r in selected
                    )
                    if group == "recovery"
                    else None,
                    "input_tokens": sum(r.get("input_tokens", 0) for r in selected),
                    "statuses": dict(Counter(r["status"] for r in selected)),
                }
            )
            groups[group][variant] = summary
    return {
        "schema_version": "1.0",
        "name": protocol.name,
        "fingerprint": fingerprint,
        "protocol": protocol.model_dump(mode="json"),
        "aggregate": summarize(rows, 240000),
        "groups": groups,
        "pairs": pairs,
        "results": rows,
        "model_snapshot": None,
        "sampling_seed": None,
        "usage_provenance": (
            "provider usage; scripted prefix and preflight rejection are known zero"
        ),
        "limitations": [
            "2 tasks x 2 repeats per group: descriptive pilot, no significance claim",
            "group tasks differ: compare A/B within group only",
            "input admission uses character-based estimates, not the provider window",
            "API snapshot and seed unreported; temperature zero is not deterministic",
            "scripted prefix measures recovery after controlled errors; "
            "spontaneous error incidence is unmeasured",
        ],
    }


def render_report(report: dict[str, Any]) -> str:
    """Compact export of functional versus efficacy evidence."""
    lines = [
        "# Harness mechanism pilot v1",
        "",
        f"Total provider tokens: {report['aggregate']['total_tokens']}; "
        f"repairs passed: {report['aggregate']['successes']}/24.",
        "",
        "| Group | Variant | Repair | Within constraints | "
        "Input tokens | Total tokens | Triggered |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for group, variants in report["groups"].items():
        for variant, data in variants.items():
            lines.append(
                f"| {group} | {variant} | {data['successes']}/4 | "
                f"{data['success_within_constraints']}/4 | {data['input_tokens']} | "
                f"{data['total_tokens']} | {data['mechanism_triggered']}/4 |"
            )
    lines.extend(
        [
            "",
            "## Paired deltas",
            "",
            "| Pair | Valid | A repair | B repair | Token delta B-A |",
            "|---|---|---|---|---:|",
        ]
    )
    lines.extend(
        f"| {pair['pair_id']} | {pair['valid']} | {pair['A_success']} | "
        f"{pair['B_success']} | {pair['total_token_delta_B_minus_A']} |"
        for pair in report["pairs"]
    )
    lines.extend(["", "## Limits", "", *("- " + x for x in report["limitations"]), ""])
    return "\n".join(lines)


def save_report(output: Path, report: dict[str, Any]) -> None:
    """Atomic checkpoint after each run; Markdown is a regenerable view."""
    write_json(output / "report.json", report)
    (output / "report.md").write_text(render_report(report))

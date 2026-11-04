"""Frozen larger-budget follow-up; v1 facts, protocols and core defaults stay intact."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.benchmark.harness_report import digest, trajectory_checks
from minidsh.benchmark.harness_runner import (
    _acceptance,
    _kernel_probe,
    _model_factory,
    _probe_stimulus,
    _progress,
    _scenario_seed,
    _snapshot,
    fingerprint,
)
from minidsh.benchmark.harness_study import (
    InputAdmissionModel,
    scenario_calls,
    scenario_history,
    study_options,
)
from minidsh.benchmark.public_lite import (
    COMMANDS,
    LiteSelection,
    LiteTask,
    Record,
    capture_candidate,
    checked_image_size,
    container_limits,
    native_platform,
    seed_container,
    source_filename,
    verify_candidate,
)
from minidsh.benchmark.public_swe import PublicStudyError, run_checked, write_json
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import (
    AgentBudget,
    JsonValue,
    LatencyBreakdown,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from minidsh.evaluation.harness_metrics import ScoringEvidence, score_harness
from minidsh.evaluation.latency import latency_from_trace
from minidsh.evaluation.tracefile import TraceRun
from minidsh.infrastructure import CommandPolicy, ProcessRunner
from minidsh.infrastructure.containers import DockerHost, DockerSandbox
from minidsh.ports import Model
from minidsh.tools import ShellTool

GROUPS = {
    "context": (
        "python_000_has_close_elements",
        "python_001_separate_paren_groups",
        "js_118_getclosestvowel",
        "js_151_doublethedifference",
    ),
    "output": (
        "python_007_filter_by_substring",
        "python_025_factorize",
        "js_062_derivative",
        "js_089_encrypt",
    ),
    "recovery": (
        "python_010_make_palindrome",
        "python_090_next_smallest",
        "js_021_rescaletounit",
        "js_026_removeduplicates",
    ),
}
PROTOCOL = Path("benchmarks/harness_study/v2/protocol.json")


class FollowupProtocol(BaseModel):
    """New cohort only: fixed tasks, estimands, repetition and budgets before API use."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["2.0"]
    name: Literal["public_harness_followup_v2"]
    selection: Literal["benchmarks/public_lite/v1/selection.json"]
    model: Literal["deepseek-flash"]
    temperature: Literal[0]
    thinking: Literal[False]
    max_output_tokens: Literal[1024]
    repeats: Literal[3]
    regression_tokens: Literal[32000]
    mechanism_tokens: Literal[64000]
    total_token_budget: Literal[6000000]
    max_steps: Literal[16]
    max_tool_calls: Literal[32]
    max_duration_seconds: Literal[180]
    bootstrap_seed: Literal[20261028]
    bootstrap_samples: Literal[2000]
    groups: dict[str, tuple[str, str, str, str]]

    @model_validator(mode="after")
    def frozen_groups(self) -> FollowupProtocol:
        if self.groups != GROUPS:
            raise ValueError("task assignment is frozen before sampling")
        return self

    @classmethod
    def from_file(cls, path: Path) -> FollowupProtocol:
        return cls.model_validate_json(path.read_text())


@dataclass(frozen=True)
class FollowupRun:
    group: str
    task_id: str
    repeat: int
    variant: str

    @property
    def id(self) -> str:
        return f"{self.group}_{self.task_id}_r{self.repeat}_{self.variant}"

    @property
    def pair_id(self) -> str:
        return f"{self.group}_{self.task_id}_r{self.repeat}"


def followup_schedule(protocol: FollowupProtocol) -> tuple[FollowupRun, ...]:
    rows: list[FollowupRun] = []
    for repeat in range(1, protocol.repeats + 1):
        for group, tasks in protocol.groups.items():
            for index, task in enumerate(tasks):
                order = ("A", "B") if (index + repeat) % 2 else ("B", "A")
                rows.extend(FollowupRun(group, task, repeat, variant) for variant in order)
        rows.extend(
            FollowupRun("regression", task, repeat, "baseline")
            for tasks in protocol.groups.values()
            for task in tasks
        )
    return tuple(rows)


def row_options(row: FollowupRun) -> RuntimeOptions:
    # Regression is the same full-history harness without synthetic history or faults.
    return (
        study_options("context", "A")
        if row.group == "regression"
        else study_options(row.group, row.variant)
    )


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [r for r in rows if r["status"] != "not_run"]
    total = sum(r.get("tokens", 0) for r in ran)
    unknown = sum(r.get("unknown_usage_calls", 0) for r in ran)
    success = sum(r["success"] is True for r in ran)
    complete = unknown == 0 and all(r.get("trace_complete", True) for r in ran)
    return {
        "declared_runs": len(rows),
        "executed_runs": len(ran),
        "not_run": len(rows) - len(ran),
        "successes": success,
        "full_successes": sum(r.get("full_success") is True for r in ran),
        "constraint_compliant": sum(r.get("constraint_compliant") is True for r in ran),
        "success_within_constraints": sum(
            r.get("full_success") is True and r.get("constraint_compliant") is True for r in ran
        ),
        "total_tokens": total,
        "unknown_usage_calls": unknown,
        "token_usage_complete": complete,
        "known_tokens_per_success": total / success if success else None,
        "tokens_per_success": total / success if success and complete else None,
        "token_overrun": sum(r.get("token_overrun", 0) for r in ran),
        "api_calls": sum(r.get("api_calls", 0) for r in ran),
    }


def paired_summary(rows: list[dict[str, Any]], group: str) -> dict[str, Any]:
    pairs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["group"] == group:
            pairs[row["pair_id"]][row["variant"]] = row
    complete = [
        (p["A"], p["B"])
        for p in pairs.values()
        if all(
            v in p and p[v]["status"] != "not_run" and p[v].get("unknown_usage_calls", 0) == 0
            for v in ("A", "B")
        )
    ]
    clusters: dict[str, list[float]] = defaultdict(list)
    for a, b in complete:
        clusters[a["task_id"]].append(float(b["tokens"] - a["tokens"]))
    means = [statistics.mean(values) for values in clusters.values()]
    interval = None
    if means:
        rng = random.Random(20261028)
        draws = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(2000))
        interval = [draws[49], draws[1949]]
    return {
        "declared_pairs": len(pairs),
        "complete_pairs": len(complete),
        "task_clusters": len(clusters),
        "triggered_pairs": sum(
            a.get("mechanism_triggered") is True and b.get("mechanism_triggered") is True
            for a, b in complete
        ),
        "both_constraint_compliant_pairs": sum(
            a.get("constraint_compliant") is True and b.get("constraint_compliant") is True
            for a, b in complete
        ),
        "mean_token_delta_B_minus_A": statistics.mean(means) if means else None,
        "token_delta_cluster_bootstrap_95": interval,
        "task_mean_token_deltas": {k: statistics.mean(v) for k, v in clusters.items()},
        "input_token_delta_B_minus_A": sum(
            b.get("input_tokens", 0) - a.get("input_tokens", 0) for a, b in complete
        )
        if complete
        else None,
        "A_only_full_success": sum(
            a.get("full_success") is True and b.get("full_success") is not True for a, b in complete
        ),
        "B_only_full_success": sum(
            b.get("full_success") is True and a.get("full_success") is not True for a, b in complete
        ),
        "uncertainty_scope": (
            "task-cluster percentile bootstrap; four selected tasks, not a population guarantee"
        ),
    }


def save_followup(output: Path, protocol: FollowupProtocol, rows: list[dict[str, Any]]) -> None:
    groups = {
        group: {
            variant: summarize_rows(
                [r for r in rows if r["group"] == group and r["variant"] == variant]
            )
            for variant in (("baseline",) if group == "regression" else ("A", "B"))
        }
        for group in ("regression", *protocol.groups)
    }
    report = {
        "schema_version": "2.0",
        "protocol": protocol.model_dump(mode="json"),
        "aggregate": summarize_rows(rows),
        "groups": groups,
        "pairs": {g: paired_summary(rows, g) for g in protocol.groups},
        "results": rows,
    }
    write_json(output / "report.json", report)
    lines = [
        "# Harness follow-up v2",
        "",
        "| Group | Variant | Executed/declared | Repair | Full | Within constraints | Tokens |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for group, variants in groups.items():
        for variant, data in variants.items():
            lines.append(
                f"| {group} | {variant} | {data['executed_runs']}/{data['declared_runs']} | "
                f"{data['successes']} | {data['full_successes']} | "
                f"{data['success_within_constraints']} | {data['total_tokens']} |"
            )
    lines.extend(
        [
            "",
            "Repair, trajectory goals, budget compliance and terminal state are separate.",
            "Fixed stimuli use zero API tokens; their steps/tools are included.",
            "Bootstrap resamples tasks, not repeated runs; no ranking or significance claim.",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines))


async def check_followup(protocol: FollowupProtocol, output: Path, host: ProcessRunner) -> None:
    if await asyncio.to_thread(output.exists):
        raise PublicStudyError("use a new directory; never overwrite a frozen cohort")
    await asyncio.to_thread(output.mkdir, parents=True)
    selection = LiteSelection.from_file(Path(protocol.selection))
    if {t.id for t in selection.tasks} != {t for tasks in protocol.groups.values() for t in tasks}:
        raise PublicStudyError("regression and mechanism task coverage mismatch")
    records = selection.records(Path("benchmark-results/public-lite/sources"))
    images = json.loads(
        await asyncio.to_thread(
            Path("benchmark-results/public-lite/baseline/images.json").read_text
        )
    )
    platform = await native_platform(host)
    if images["platform"] != platform:
        raise PublicStudyError("native image architecture mismatch")
    for image in images["images"].values():
        checked_image_size(
            await run_checked(host, ("docker", "image", "inspect", image)),
            image,
            platform.split("/")[1],
        )
    _snapshot(PROTOCOL, output, images)
    write_json(output / "images.json", images)
    acceptance = await _acceptance(output)
    if not all(r["passed"] for r in acceptance) or not await _kernel_probe(host, images, output):
        raise PublicStudyError("functional or actual isolation gate failed")
    write_json(output / "acceptance.json", acceptance)
    gates = []
    for task in selection.tasks:
        record = records[task.id]
        outcomes = [
            await verify_candidate(
                host,
                task=task,
                record=record,
                candidate=record.prompt + code,
                image=images["images"][task.language],
                platform=platform,
                logs=output / "references" / task.id / label,
            )
            for label, code in (
                ("gold", record.canonical_solution),
                ("buggy", record.buggy_solution),
            )
        ]
        group = next(g for g, tasks in protocol.groups.items() if task.id in tasks)
        stimuli = [
            await _probe_stimulus(host, images, output, task, record, group, variant)
            for variant in ("A", "B")
        ]
        if outcomes != [True, False] or not all(stimuli):
            raise PublicStudyError("reference or stimulus gate failed: " + task.id)
        gates.append(
            {
                "task": task.id,
                "gold_pass": outcomes[0],
                "buggy_pass": outcomes[1],
                "stimuli": stimuli,
            }
        )
        _progress({"gate_task": task.id, "passed": True})
    changes = {}
    for group in protocol.groups:
        a, b = (asdict(study_options(group, variant)) for variant in ("A", "B"))
        changed = [k for k in a if a[k] != b[k]]
        expected = {
            "context": "context_strategy",
            "output": "tool_output_limit",
            "recovery": "error_recovery_enabled",
        }[group]
        if changed != [expected]:
            raise PublicStudyError("single-variable gate failed")
        changes[group] = changed
    await rehearsal(protocol, selection, images, output, host)
    receipts = await asyncio.to_thread(artifact_hashes, output)
    write_json(
        output / "gates.json",
        {
            "fingerprint": fingerprint(PROTOCOL, images),
            "receipts": receipts,
            "tasks": gates,
            "single_variable_options": changes,
            "provider_calls": 0,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )


def artifact_hashes(output: Path) -> dict[str, str]:
    return {
        str(p.relative_to(output)): digest(p)
        for p in sorted(output.rglob("*"))
        if p.is_file() and "reproduction" not in p.parts
    }


def latency_reconciled(latency: LatencyBreakdown) -> bool:
    """Trace latency components are rounded separately to three decimal places."""
    return math.isclose(
        latency.total_ms,
        latency.model_ms + latency.tool_ms + latency.harness_ms,
        rel_tol=0.0,
        abs_tol=0.002,
    )


async def rehearsal(
    protocol: FollowupProtocol,
    selection: LiteSelection,
    images: dict[str, Any],
    output: Path,
    host: ProcessRunner,
) -> None:
    """Exercise the full exporter/scorer with synthetic fixtures before reading any key."""
    for language, group in (("python", "output"), ("js", "recovery")):
        template = next(t for t in selection.tasks if t.language == language)
        task = template.model_copy(update={"entry_point": "add"})
        python = language == "python"
        prompt = "def add(a, b):\n" if python else "function add(a, b) {\n"
        fixed = "    return a + b\n" if python else "return a + b;\n}\n"
        test = "assert add(2, 3) == 5" if python else "console.assert(add(2, 3) === 5);"
        record = Record(
            task_id=task.upstream_id,
            prompt=prompt,
            buggy_solution="    return 0\n" if python else "return 0;\n}\n",
            canonical_solution=fixed,
            example_test=test,
            test=test,
            docstring="Return the sum of two numbers.",
            entry_point="add",
            bug_type="synthetic_rehearsal",
        )
        filename = source_filename(task.language)
        if python:
            command = [
                "python3",
                "-c",
                "import os;from pathlib import Path;"
                f"Path('next.py').write_text({(prompt + fixed)!r});"
                f"os.replace('next.py',{filename!r})",
            ]
        else:
            command = [
                "node",
                "-e",
                "const fs=require('node:fs');"
                f"fs.writeFileSync('next.js',{json.dumps(prompt + fixed)});"
                f"fs.renameSync('next.js',{json.dumps(filename)})",
            ]
        smoke = ["python3", "smoke.py"] if python else ["node", "smoke.js"]
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("patch", "shell", {"argv": list[JsonValue](command)}),),
                    usage=TokenUsage(),
                ),
                ModelResponse(
                    tool_calls=(ToolCall("smoke", "shell", {"argv": list[JsonValue](smoke)}),),
                    usage=TokenUsage(),
                ),
                ModelResponse("Fixed. regression_head_v1", usage=TokenUsage()),
            ]
        )
        row = await execute_row(
            FollowupRun(group, task.id, 1, "B"),
            protocol,
            task,
            record,
            images,
            output / "rehearsal",
            host,
            model,
            64000,
        )
        if not row["full_success"] or not row["constraint_compliant"] or row["tokens"] != 0:
            raise PublicStudyError("synthetic end-to-end rehearsal failed: " + language)
        if group == "recovery" and row["recovered_in_window"] is not True:
            raise PublicStudyError("synthetic recovery checkpoint failed")
    write_json(
        output / "rehearsal.json",
        {"synthetic_cases": 2, "provider_calls": 0, "provider_tokens": 0, "passed": True},
    )


async def execute_row(  # noqa: PLR0913, PLR0917 - trusted experimental boundary
    key: FollowupRun,
    protocol: FollowupProtocol,
    task: LiteTask,
    record: Record,
    images: dict[str, Any],
    output: Path,
    host: ProcessRunner,
    model: Model,
    remaining: int,
) -> dict[str, Any]:
    directory = output / "runs" / key.id
    seed, goal = _scenario_seed(directory, task, record, key.group)
    history = scenario_history(goal) if key.group == "context" else ()
    observed = InputAdmissionModel(model, scenario_calls(key.group, task.language))
    nominal = protocol.regression_tokens if key.group == "regression" else protocol.mechanism_tokens
    budget = AgentBudget(
        max_steps=protocol.max_steps,
        max_tokens=min(nominal, remaining),
        max_tool_calls=protocol.max_tool_calls,
        max_duration_seconds=protocol.max_duration_seconds,
    )
    run_id = uuid.uuid4().hex
    async with DockerSandbox(
        host, image=images["images"][task.language], resources=container_limits(images["platform"])
    ) as box:
        await seed_container(host, box, seed)
        runner = box.runner("/app", CommandPolicy(COMMANDS))
        runtime = build_local_runtime(
            workspace=directory,
            model=observed,
            budget=budget,
            options=row_options(key),
            components=RuntimeComponents(runner=runner, tools=(ShellTool(runner),)),
        )
        runtime.events.subscribe(
            "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
        )
        try:
            async with asyncio.timeout(protocol.max_duration_seconds):
                await runtime.agent.run(goal, run_id=run_id, prior_history=history)
        except TimeoutError:
            pass  # The original cancelled trace and unknown in-flight usage remain authoritative.
        candidate = await capture_candidate(box, task.language) if box.alive else None
    success = False
    verification_started = time.monotonic()
    if candidate is not None:
        (directory / source_filename(task.language)).write_text(candidate)
        success = await verify_candidate(
            host,
            task=task,
            record=record,
            candidate=candidate,
            image=images["images"][task.language],
            platform=images["platform"],
            logs=directory / "verifier",
        )
    verification_ms = (time.monotonic() - verification_started) * 1000
    trace = TraceRun.from_file(directory / "trace.jsonl", run_id=run_id)
    inputs, outputs, cached = trace.total_tokens()
    checks = (
        trajectory_checks(trace, key.group, key.variant, task.language, success=success)
        if key.group != "regression"
        else {}
    )
    goals = {"repair": success}
    constraints: dict[str, bool | None] = {"usage_known": observed.unknown_calls == 0}
    if key.group == "context":
        constraints["pinned_critical_fact"] = checks.get("pinned_critical_fact") is True
    if key.group == "output":
        goals["diagnosis"] = checks.get("diagnosis_passed") is True
        constraints["diagnostic_retained"] = (
            checks.get("diagnostic_retained_in_observation") is True
        )
    evidence = ScoringEvidence.model_validate(
        {
            "run_id": run_id,
            "case_id": key.id,
            "source": "sealed_public_verifier_and_rules_v2",
            "task_success": success,
            "goal_checks": goals,
            "constraint_checks": constraints,
            "budget": asdict(budget),
            "recovery_checks": {"injected_missing_file_episode": checks["recovered_in_window"]}
            if key.group == "recovery"
            else {},
        }
    )
    # ScoringBudget names differ from the runtime duration name only in type, not semantics.
    metrics = score_harness(trace, evidence=evidence)
    latency = latency_from_trace(trace)
    final = trace.final()
    if final is not None and final.data.get("tokens") != inputs + outputs:
        raise PublicStudyError("terminal and per-call token accounting disagree")
    if not latency_reconciled(latency):
        raise PublicStudyError("latency attribution does not reconcile")
    unknown = max(
        observed.unknown_calls, int(str(metrics.measurements["unknown_usage_calls"] or 0))
    )
    constraint_rate = metrics.rates["constraint_satisfied_run_rate"]
    row = dict(
        asdict(key),
        id=key.id,
        pair_id=key.pair_id,
        run_id=run_id,
        status=trace.status(),
        success=success,
        full_success=all(goals.values()),
        constraint_compliant=constraint_rate.value == 1.0 and unknown == 0,
        input_tokens=inputs,
        output_tokens=outputs,
        cached_input_tokens=cached,
        tokens=inputs + outputs,
        unknown_usage_calls=unknown,
        api_calls=observed.api_calls,
        scripted_prefix_calls=observed.prefix_calls,
        admission_rejections=observed.rejections,
        steps=final.data.get("steps") if final else None,
        tool_calls=final.data.get("tool_calls") if final else None,
        duration_ms=final.data.get("duration_ms") if final else None,
        failure_reason=trace.failure_reason(),
        token_overrun=max(0, inputs + outputs - budget.max_tokens),
        trace_complete=final is not None,
        model_versions=[
            {
                "provider_model": r.data.get("provider_model"),
                "system_fingerprint": r.data.get("system_fingerprint"),
            }
            for r in trace.by_name("model_call_completed")
            if r.data.get("provider_model") or r.data.get("system_fingerprint")
        ],
        failed_model_calls=len(trace.by_name("model_call_failed")),
        accounting_reconciled=True,
        independent_verification_ms=verification_ms if candidate is not None else None,
        latency=latency.to_json(),
        independent_verification_scope="fresh container; immutable hidden tests; candidate only",
        **checks,
    )
    write_json(directory / "evidence.json", evidence.model_dump(mode="json"))
    write_json(directory / "metrics.json", metrics.to_json())
    write_json(
        directory / "facts.json",
        {
            "row": row,
            "budget": asdict(budget),
            "runtime_options": json.loads(
                json.dumps(
                    asdict(row_options(key)),
                    default=lambda value: (
                        sorted(value) if isinstance(value, frozenset) else str(value)
                    ),
                )
            ),
            "trace_sha256": digest(directory / "trace.jsonl"),
            "candidate_sha256": digest(directory / source_filename(task.language))
            if candidate is not None
            else None,
            "verifier_sha256": digest(directory / "verifier/result.json")
            if candidate is not None
            else None,
            "evidence_sha256": digest(directory / "evidence.json"),
        },
    )
    return row


async def run_followup(
    protocol: FollowupProtocol, output: Path, host: ProcessRunner, credentials_file: Path
) -> None:
    if (output / "report.json").exists() or (output / "runs").exists():
        raise PublicStudyError("existing cohort cannot be overwritten or selectively rerun")
    gates = json.loads((output / "gates.json").read_text())
    images = json.loads((output / "images.json").read_text())
    if gates["fingerprint"] != fingerprint(PROTOCOL, images):
        raise PublicStudyError("source or protocol changed after gates")
    for name, expected in gates["receipts"].items():
        if digest(output / name) != expected:
            raise PublicStudyError("gate evidence changed: " + name)
    if images["platform"] != await native_platform(host):
        raise PublicStudyError("Docker architecture changed")
    selection = LiteSelection.from_file(Path(protocol.selection))
    tasks = {task.id: task for task in selection.tasks}
    records = selection.records(Path("benchmark-results/public-lite/sources"))
    keys = followup_schedule(protocol)
    rows = [
        dict(asdict(k), id=k.id, pair_id=k.pair_id, status="not_run", success=False, tokens=0)
        for k in keys
    ]
    model = _model_factory(credentials_file)
    consumed = 0
    stopped = False
    save_followup(output, protocol, rows)
    for index, key in enumerate(keys):
        if stopped or consumed >= protocol.total_token_budget:
            rows[index]["failure_reason"] = (
                "unknown usage/infrastructure stop" if stopped else "aggregate budget"
            )
            continue
        try:
            row = await execute_row(
                key,
                protocol,
                tasks[key.task_id],
                records[key.task_id],
                images,
                output,
                host,
                model,
                protocol.total_token_budget - consumed,
            )
        except (OSError, ValueError, PublicStudyError) as exc:
            row = rows[index] | {
                "status": "infrastructure_error",
                "trace_complete": False,
                "failure_reason": type(exc).__name__,
                "unknown_usage_calls": 1,
            }
            path = output / "runs" / key.id / "trace.jsonl"
            if path.exists():
                inputs, outputs, _ = TraceRun.from_file(path).total_tokens()
                row["tokens"] = inputs + outputs
            stopped = True
        rows[index] = row
        consumed += row["tokens"]
        stopped |= row.get("unknown_usage_calls", 0) > 0
        save_followup(output, protocol, rows)
        _progress(
            {
                "index": index + 1,
                "declared": len(rows),
                "run": key.id,
                "status": row["status"],
                "repair": row["success"],
                "full": row.get("full_success"),
                "tokens": row["tokens"],
                "total_tokens": consumed,
            },
        )
    save_followup(output, protocol, rows)
    write_json(
        output / "audit.json",
        {
            "source_unchanged": gates["fingerprint"] == fingerprint(PROTOCOL, images),
            "files": await asyncio.to_thread(artifact_hashes, output),
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, default=Path(".env.deepseek"))
    args = parser.parse_args()
    output = args.output.resolve()
    root = await asyncio.to_thread(Path("benchmark-results").resolve)
    if not output.is_relative_to(root):
        raise PublicStudyError("experiment output must be inside benchmark-results")
    protocol = FollowupProtocol.from_file(PROTOCOL)
    host = DockerHost()
    if args.command == "check":
        await check_followup(protocol, output, host)
    else:
        await run_followup(protocol, output, host, args.credentials_file)


if __name__ == "__main__":
    asyncio.run(main())

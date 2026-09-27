# ruff: noqa: RUF001
# The generated Markdown report uses Chinese punctuation.
"""Reproduce offline baselines, ablations, journal pairs and Trace audits.

Derived from the four local scripts used for the 2026-09-27 offline report.
Raw reports remain under benchmark-results; no real model is constructed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from minidsh.adapters import ScriptedModel
from minidsh.adapters.session_journal import JsonlSessionJournal
from minidsh.application.session_journal import MemorySessionJournal
from minidsh.benchmark.runner import BenchmarkManifest, BenchmarkRunner, BenchmarkTask
from minidsh.cli import _task_options
from minidsh.composition import (
    LocalRuntime,
    RuntimeComponents,
    RuntimeOptions,
    build_local_runtime,
)
from minidsh.domain import ModelPricing
from minidsh.evaluation import TraceRun, build_replay

PROJECT = Path(__file__).resolve().parent.parent
BASELINES = ("smoke", "core", "stress")
ABLATIONS = ("context", "tool_output", "permission")
JOURNAL_TRIALS = 4
REPEATS = 2
STABLE = (
    "success",
    "status",
    "steps",
    "tokens",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "tool_calls",
    "cost_usd",
    "permission_denied",
    "unsafe_blocked",
    "failure_type",
    "failure_category",
    "failure_layer",
    "retries",
    "retry_successes",
    "verifier_exit_code",
)
AGGREGATES = (
    "success_rate",
    "average_steps",
    "average_tokens",
    "average_tool_calls",
    "average_cost_usd",
    "cost_per_success_usd",
    "pass_at_k",
    "retry_success_rate",
    "cached_input_share",
)
MODEL: dict[str, Any] = {
    "label": "scripted",
    "pricing": {"input_usd_per_million": 3, "output_usd_per_million": 15},
}


class RegressionError(RuntimeError):
    """An offline experiment failed its executable contract."""


def clean_environment() -> dict[str, str]:
    """Pass only process essentials, excluding credentials and implicit configuration."""
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def source_fingerprint(project: Path) -> str:
    """Hash implementation, protocol tests, experiment scripts and pinned inputs."""
    files: set[Path] = set()
    for name in ("src", "tests", "scripts", "benchmarks", ".github", "docs/adr"):
        files.update(path for path in (project / name).rglob("*") if path.is_file())
    for name in (
        "pyproject.toml",
        "uv.lock",
        "AGENTS.md",
        ".gitignore",
        "docs/results/summary.json",
    ):
        if (project / name).is_file():
            files.add(project / name)
    digest = hashlib.sha256()
    for path in sorted(files):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(path.relative_to(project).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    """Read a machine report; invalid or missing data is a hard failure."""
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a report within the newly allocated experiment directory."""
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def compare_repeats(results: Sequence[Mapping[str, Any]], *, repeats: int) -> int:
    """Require successful criteria and identical deterministic metrics per task."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        if not result["success"]:
            raise RegressionError(f"criterion failed: {result['task_id']}")
        grouped[result["task_id"]].append(result)
    if not grouped:
        raise RegressionError("empty experiment results")
    for task_id, runs in grouped.items():
        if len(runs) != repeats:
            raise RegressionError(f"repeat count mismatch: {task_id}")
        for run in runs[1:]:
            changed = [key for key in STABLE if run.get(key) != runs[0].get(key)]
            if changed:
                raise RegressionError(f"repeat metrics changed: {task_id}: {changed}")
    return len(grouped)


def run_command(argv: list[str], output: Path, name: str) -> dict[str, Any]:
    """Execute a fixed argv in a scrubbed environment with a bounded timeout."""
    started = time.monotonic()
    with (output / f"{name}.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            argv,
            cwd=PROJECT,
            env=clean_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
    record = {
        "name": name,
        "argv": argv,
        "exit_code": result.returncode,
        "duration_seconds": time.monotonic() - started,
    }
    sys.stdout.write(f"{name}: exit={result.returncode}\n")
    sys.stdout.flush()
    if result.returncode:
        raise RegressionError(f"{name} failed; inspect {output / (name + '.log')}")
    return record


def run_baselines(output: Path, metadata: dict[str, Any]) -> None:
    """Run the original three baselines and six single-variable ablations."""
    config = output / "empty.toml"
    config.write_text("# No implicit runtime profiles.\n", encoding="utf-8")
    common = [
        "--config",
        str(config),
        "--provider",
        "scripted",
        "--no-multi-agent",
        "--allow-write",
        "--allow-shell",
        "--allow-command",
        "python",
        "--verify-command",
        "python",
        "--input-usd-per-million",
        "3",
        "--output-usd-per-million",
        "15",
        "--repeat",
        str(REPEATS),
    ]
    jobs: list[tuple[str, str, str | None]] = [("benchmark", suite, None) for suite in BASELINES]
    jobs += [
        ("ablation", suite, ablation) for suite in ("core", "stress") for ablation in ABLATIONS
    ]
    for command, suite, ablation in jobs:
        name = suite if ablation is None else f"{suite}-{ablation}"
        argv = [
            sys.executable,
            "-m",
            "minidsh",
            command,
            f"benchmarks/{suite}/manifest.json",
            *common,
            "--output",
            str(output / f"{name}.json"),
        ]
        if ablation is not None:
            argv += ["--suite", ablation]
        record = run_command(argv, output, name)
        record["task_count"] = len(
            BenchmarkManifest.from_file(PROJECT / "benchmarks" / suite / "manifest.json").tasks
        )
        metadata["commands"].append(record)
        write_json(output / "metadata.json", metadata)


def journal_factory(backend: str, workspace: Path, task: BenchmarkTask) -> LocalRuntime:
    """Keep everything except the journal identical between paired runs."""
    if task.response_script is None:
        raise RegressionError("journal experiment requires a scripted task")
    journal = MemorySessionJournal() if backend == "memory" else JsonlSessionJournal(workspace)
    options = RuntimeOptions(
        allow_write=True,
        allow_shell=True,
        allowed_commands=frozenset({"git", "python"}),
        pricing=ModelPricing(3, 15),
    )
    return build_local_runtime(
        workspace=workspace,
        model=ScriptedModel.from_file(task.response_script),
        options=_task_options(options, task),
        budget=task.budget,
        components=RuntimeComponents(journal=journal),
    )


async def run_journals(output: Path) -> None:
    """Alternate backend order over four trials; run in a scrubbed worker process."""
    records = []
    pairs = []
    for trial in range(JOURNAL_TRIALS):
        order = ("memory", "jsonl") if trial % 2 == 0 else ("jsonl", "memory")
        for suite in ("core", "stress"):
            selected: dict[str, dict[str, Any]] = {}
            manifest = BenchmarkManifest.from_file(PROJECT / "benchmarks" / suite / "manifest.json")
            for backend in order:
                path = output / f"journal-{suite}-{trial}-{backend}.json"
                runner = BenchmarkRunner(
                    partial(journal_factory, backend),
                    path.with_suffix(""),
                    verification_allowlist=frozenset({"python"}),
                    model=MODEL,
                )
                report = await runner.run(manifest)
                path.write_text(report.to_json() + "\n", encoding="utf-8")
                payload = load_json(path)
                selected[backend] = payload
                records.append(
                    {
                        "trial": trial,
                        "manifest": suite,
                        "backend": backend,
                        "file": path.name,
                        **payload,
                    }
                )
            for memory, disk in zip(
                selected["memory"]["results"],
                selected["jsonl"]["results"],
                strict=True,
            ):
                if memory["task_id"] != disk["task_id"]:
                    raise RegressionError("journal task order mismatch")
                changed = [key for key in STABLE if memory.get(key) != disk.get(key)]
                if changed:
                    raise RegressionError(f"journal metrics changed: {changed}")
                pairs.append(
                    {
                        "trial": trial,
                        "manifest": suite,
                        "task_id": memory["task_id"],
                        "memory_harness_ms": memory["harness_ms"],
                        "jsonl_harness_ms": disk["harness_ms"],
                        "harness_delta_ms": disk["harness_ms"] - memory["harness_ms"],
                    }
                )
    write_json(
        output / "journal-ablation.json",
        {
            "schema_version": "1.0",
            "model": "scripted",
            "trials": JOURNAL_TRIALS,
            "only_changed_variable": "SessionJournal implementation (memory vs JSONL+fsync)",
            "order": "alternated across trials",
            "records": records,
            "paired_tasks": pairs,
        },
    )


def check_archive(
    name: str, variant: str, aggregate: dict[str, Any], archive: dict[str, Any]
) -> None:
    """Compare historical deterministic metrics, normalizing cumulative repeats."""
    if variant == "baseline":
        prior = archive["baselines"][name]["aggregate"]
    else:
        prior = next(
            item["aggregate"]
            for item in archive["ablations"][name]["variants"]
            if item["name"] == variant
        )
    for key in (
        *AGGREGATES,
        "permission_denied",
        "unsafe_blocked",
        "cached_input_tokens",
        "total_cost_usd",
    ):
        current = aggregate[key] if key in AGGREGATES else aggregate[key] / REPEATS
        if not math.isclose(current, prior[key], rel_tol=1e-12, abs_tol=1e-12):
            raise RegressionError(f"archived metric changed: {name}/{variant}/{key}")


def audit(output: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on criteria, repeat metrics, journal pairing or Trace replay drift."""
    archive = load_json(PROJECT / "docs/results/summary.json")
    groups = []
    all_results = []
    repeated_tasks = 0
    for command in metadata["commands"]:
        payload = load_json(output / f"{command['name']}.json")
        if payload["model"]["label"] != "scripted":
            raise RegressionError("unexpected model in offline report")
        variants = payload.get(
            "variants",
            [
                {
                    "configuration": {"name": "baseline"},
                    "aggregate": payload.get("aggregate"),
                    "results": payload.get("results"),
                }
            ],
        )
        for variant in variants:
            count = compare_repeats(variant["results"], repeats=REPEATS)
            if count != command["task_count"]:
                raise RegressionError("missing tasks in experiment report")
            repeated_tasks += count
            check_archive(
                command["name"], variant["configuration"]["name"], variant["aggregate"], archive
            )
            groups.append(
                {
                    "experiment": command["name"],
                    "variant": variant["configuration"]["name"],
                    "aggregate": variant["aggregate"],
                }
            )
            all_results.extend(variant["results"])
    journal = load_json(output / "journal-ablation.json")
    for record in journal["records"]:
        compare_repeats(record["results"], repeats=1)
        all_results.extend(record["results"])
    for result in all_results:
        replay = build_replay(TraceRun.from_file(Path(result["trace_path"])))
        tokens = (result["input_tokens"], result["output_tokens"], result["cached_input_tokens"])
        if (
            replay.status != result["status"]
            or replay.tokens != tokens
            or not math.isclose(replay.cost_usd, result["cost_usd"], abs_tol=1e-12)
        ):
            raise RegressionError(f"Trace replay changed: {result['trace_path']}")
    pairs = journal["paired_tasks"]
    return {
        "schema_version": "1.0",
        "total_task_runs": len(all_results),
        "passed_task_runs": len(all_results),
        "repeated_task_pairs": repeated_tasks,
        "journal_paired_tasks": len(pairs),
        "groups": groups,
        "median_journal_harness_delta_ms": statistics.median(
            pair["harness_delta_ms"] for pair in pairs
        ),
        "repeat_metric_mismatches": [],
        "archived_metric_mismatches": [],
        "trace_replay_mismatches": [],
        "journal_stable_metric_mismatches": 0,
    }


def emit_report(output: Path, metadata: dict[str, Any], summary: dict[str, Any]) -> None:
    """Generate measured results without asserting unrecorded quality checks."""
    lines = [
        "# 离线可复现基线",
        "",
        "> 生成来源：`scripts/offline_regression.py`；输入为同目录 JSON 报告与 Trace。",
        "",
        f"- Git HEAD：`{metadata['git_head']}`；工作区有改动：`{metadata['dirty_workspace']}`。",
        f"- 源码、测试、实验脚本与输入摘要：`{metadata['source_sha256']}`。",
        "- 模型：ScriptedModel；无真实模型调用，实际 API 费用 $0。",
        "- 成本为输入 $3 / 输出 $15 per 1M tokens 的模拟值，不代表账单。",
        "- 显式空配置、单 Agent、本地后端、默认工具断网；凭据及 profile 环境变量不传入。",
        "- 墙钟耗时允许波动；脚本结果不能证明真实模型任务能力。",
        "",
        "| 实验 | 变体 | 执行次数 | 判据通过 | 平均估算 Token |",
        "| --- | --- | --- | --- | --- |",
    ]
    for group in summary["groups"]:
        value = group["aggregate"]
        lines.append(
            f"| {group['experiment']} | {group['variant']} | {value['runs']} | "
            f"{value['success_rate']:.0%} | {value['average_tokens']:,.2f} |"
        )
    lines += [
        "",
        f"共 {summary['total_task_runs']} 次判据通过；"
        f"{summary['repeated_task_pairs']} 对重复任务确定性指标一致。",
        f"Session 对照 {summary['journal_paired_tasks']} 对，确定性指标一致；"
        f"Harness 配对耗时增量中位数 {summary['median_journal_harness_delta_ms']:.3f} ms。",
        "全部 Trace 回放终态、Token、模拟成本与报告一致；确定性指标与历史归档一致。",
        "",
        "复现命令：",
        "",
        "```bash",
        "uv sync --locked --all-groups",
        "uv run python scripts/offline_regression.py",
        "```",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """Allocate a fresh output directory and complete the experiment and audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New directory under benchmark-results")
    parser.add_argument("--journal-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.journal_worker is not None:
        asyncio.run(run_journals(args.journal_worker))
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or PROJECT / "benchmark-results" / f"offline-{stamp}").resolve()
    if not output.is_relative_to(PROJECT / "benchmark-results"):
        parser.error("--output must be inside benchmark-results")
    output.mkdir(parents=True, exist_ok=False)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True, timeout=10
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=PROJECT,
            text=True,
            timeout=10,
        ).strip()
    )
    metadata: dict[str, Any] = {
        "schema_version": "1.0",
        "model": "scripted",
        "real_model_api_calls": 0,
        "actual_api_cost_usd": 0,
        "simulated_rate_card": MODEL["pricing"],
        "git_head": head,
        "dirty_workspace": dirty,
        "source_sha256": source_fingerprint(PROJECT),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": datetime.now(UTC).isoformat(),
        "repeats": REPEATS,
        "journal_trials": JOURNAL_TRIALS,
        "commands": [],
        "status": "running",
        "manifests": {
            suite: hashlib.sha256(
                (PROJECT / "benchmarks" / suite / "manifest.json").read_bytes()
            ).hexdigest()
            for suite in BASELINES
        },
    }
    write_json(output / "metadata.json", metadata)
    try:
        run_baselines(output, metadata)
        metadata["journal_command"] = run_command(
            [sys.executable, str(Path(__file__).resolve()), "--journal-worker", str(output)],
            output,
            "journal",
        )
        summary = audit(output, metadata)
        if source_fingerprint(PROJECT) != metadata["source_sha256"]:
            raise RegressionError("experiment source changed during execution")
        write_json(output / "audit-summary.json", summary)
        emit_report(output, metadata, summary)
        metadata["status"] = "passed"
    except (RegressionError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        metadata["status"] = "failed"
        raise
    finally:
        metadata["finished_at"] = datetime.now(UTC).isoformat()
        write_json(output / "metadata.json", metadata)
    sys.stdout.write(f"REPORT: {output / 'report.md'}\n")


if __name__ == "__main__":
    main()

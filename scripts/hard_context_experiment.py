"""Compare full history and summary compaction with balanced, interleaved trials.

Requires DEEPSEEK_API_KEY in the process environment, plus explicitly declared rates.
This experiment entry point uses the existing single-agent BenchmarkRunner unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from minidsh.adapters import CredentialRedactingModel
from minidsh.adapters.profiles import BUILTIN_PROFILES, build_provider_model
from minidsh.benchmark.ablation import AblationReport, HarnessVariant, Runner, VariantReport
from minidsh.benchmark.runner import (
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkTask,
)
from minidsh.cli import task_options
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelPricing
from minidsh.evaluation import TraceRun, build_replay

PROJECT = Path(__file__).resolve().parent.parent
VARIANTS = (
    HarnessVariant("full_history", context_strategy="full"),
    HarnessVariant("summary_compaction", context_strategy="summary"),
)
type Trial = tuple[int, BenchmarkTask, HarnessVariant]
type TrialFactory = Callable[[int, BenchmarkTask, HarnessVariant], Runner]


def schedule_trials(tasks: tuple[BenchmarkTask, ...], *, repeats: int) -> tuple[Trial, ...]:
    """Alternate first strategy by task and trial; preserve the pinned task objects."""
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if not tasks:
        raise ValueError("empty task collection")
    return tuple(
        (trial, task, variant)
        for trial in range(repeats)
        for index, task in enumerate(tasks)
        for variant in (VARIANTS if (trial + index) % 2 == 0 else tuple(reversed(VARIANTS)))
    )


async def run_trials(
    manifest: BenchmarkManifest,
    *,
    repeats: int,
    runner_factory: TrialFactory,
) -> AblationReport:
    """Execute each pair sequentially; preserve failures as experimental outcomes."""
    reports: dict[str, list[BenchmarkReport]] = {variant.name: [] for variant in VARIANTS}
    for trial, task, variant in schedule_trials(manifest.tasks, repeats=repeats):
        selected = replace(manifest, tasks=(task,))
        report = await runner_factory(trial, task, variant).run(selected)
        if len(report.results) != 1 or report.results[0].task_id != task.task_id:
            raise ValueError("runner must report exactly one matching task")
        indexed = replace(report, results=(replace(report.results[0], run_index=trial),))
        reports[variant.name].append(indexed)
    return AblationReport(
        manifest.name,
        tuple(
            VariantReport(
                variant,
                BenchmarkReport(
                    manifest.name,
                    manifest.schema_version,
                    tuple(result for report in reports[variant.name] for result in report.results),
                    model=reports[variant.name][0].model,
                ),
            )
            for variant in VARIANTS
        ),
    )


def source_fingerprint() -> str:
    """Use the same version fingerprint as the offline baseline entry point."""
    spec = importlib.util.spec_from_file_location(
        "offline", PROJECT / "scripts/offline_regression.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value: str = module.source_fingerprint(PROJECT)
    return value


def write_json(path: Path, value: object) -> None:
    """Persist experiment conditions and outcomes, never credentials."""
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def progress(message: str) -> None:
    """Emit bounded progress, without raw model text or credentials."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def audit_results(report: AblationReport, output: Path, secret: str) -> None:
    """Validate execution evidence and ensure the actual credential was not persisted."""
    results = [result for variant in report.variants for result in variant.report.results]
    for result in results:
        replay = build_replay(TraceRun.from_file(Path(result.trace_path)))
        if (
            replay.status != result.status
            or replay.tokens
            != (result.input_tokens, result.output_tokens, result.cached_input_tokens)
            or not math.isclose(replay.cost_usd, result.cost_usd, abs_tol=1e-12)
        ):
            raise RuntimeError("Trace replay disagrees with experiment report")
    for path in output.rglob("*"):
        if (
            path.is_file()
            and path.suffix in {".json", ".jsonl", ".md", ".log"}
            and secret.encode() in path.read_bytes()
        ):
            raise RuntimeError("credential found in experiment artifact")
    write_json(
        output / "audit-summary.json",
        {
            "schema_version": "1.0",
            "task_runs": len(results),
            "criteria_passed": sum(result.success for result in results),
            "trace_replays_passed": len(results),
            "credential_matches": 0,
        },
    )


def parse_arguments() -> argparse.Namespace:
    """Require an output destination and operator-declared pricing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--input-usd-per-million", type=float, required=True)
    parser.add_argument("--output-usd-per-million", type=float, required=True)
    parser.add_argument("--cached-input-usd-per-million", type=float, required=True)
    return parser.parse_args()


async def main() -> None:
    """Pin conditions, execute the ordered schedule and save partial progress."""
    args = parse_arguments()
    secret = os.environ.get("DEEPSEEK_API_KEY", "")
    if not secret:
        raise ValueError("DEEPSEEK_API_KEY is required in the process environment")
    manifest = BenchmarkManifest.from_file(PROJECT / "benchmarks/hard/manifest.json")
    schedule = schedule_trials(manifest.tasks, repeats=args.repeat)
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT / "benchmark-results"):
        raise ValueError("output must be inside benchmark-results")
    status = await asyncio.to_thread(
        subprocess.check_output,
        ["git", "status", "--porcelain"],
        cwd=PROJECT,
        text=True,
        timeout=10,
    )
    dirty = bool(status.strip())
    if dirty:
        raise ValueError("commit the implementation before running this experiment")
    output.mkdir(parents=True, exist_ok=False)
    pricing = ModelPricing(
        args.input_usd_per_million,
        args.output_usd_per_million,
        args.cached_input_usd_per_million,
    )
    profile = replace(
        BUILTIN_PROFILES["deepseek"],
        model="deepseek-flash",
        thinking=False,
        pricing=pricing,
        pricing_source="Explicit experiment arguments; verify provider tariff",
        pricing_as_of=datetime.now(UTC).date().isoformat(),
    )
    conditions: dict[str, object] = {
        "label": profile.model,
        "profile": profile.describe(),
        "pricing": asdict(pricing),
    }
    digest = source_fingerprint()
    metadata = {
        "schema_version": "1.0",
        "git_head": (
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "rev-parse", "HEAD"],
                cwd=PROJECT,
                text=True,
                timeout=10,
            )
        ).strip(),
        "dirty_workspace": dirty,
        "source_sha256": digest,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "only_changed_variable": "context_strategy",
        "repeat": args.repeat,
        "model": conditions,
        "temperature": 0,
        "thinking": False,
        "recent_messages": 12,
        "summary_max_chars": 4000,
        "journal": "JsonlSessionJournal",
        "max_model_attempts": 3,
        "schedule": [
            {"trial": trial, "task_id": task.task_id, "variant": variant.name}
            for trial, task, variant in schedule
        ],
    }
    write_json(output / "metadata.json", metadata)

    def factory(trial: int, task: BenchmarkTask, variant: HarnessVariant) -> Runner:
        name = f"{trial}-{task.task_id}-{variant.name}"
        progress(f"START {name}")
        options = RuntimeOptions(
            context_strategy=variant.context_strategy,
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({"git", "python"}),
            pricing=pricing,
        )

        def runtime(workspace: Path, selected: BenchmarkTask) -> LocalRuntime:
            model = CredentialRedactingModel(
                build_provider_model(
                    profile,
                    environ={"DEEPSEEK_API_KEY": secret},
                    temperature=0,
                    max_attempts=3,
                ),
                secret,
            )
            return build_local_runtime(
                workspace=workspace,
                model=model,
                options=task_options(options, selected),
                budget=selected.budget,
            )

        runner = BenchmarkRunner(
            runtime,
            output / name,
            verification_allowlist=frozenset({"python"}),
            model=conditions,
        )

        class ProgressRunner:
            async def run(self, selected: BenchmarkManifest) -> BenchmarkReport:
                report = await runner.run(selected)
                (output / f"{name}.json").write_text(report.to_json() + "\n", encoding="utf-8")
                result = report.results[0]
                progress(f"DONE {name} success={result.success} tokens={result.tokens}")
                if result.failure_category == "provider_error":
                    raise RuntimeError("provider failure; stop and retain partial results")
                return report

        return ProgressRunner()

    try:
        report = await run_trials(manifest, repeats=args.repeat, runner_factory=factory)
        if source_fingerprint() != digest:
            raise RuntimeError("implementation changed during evaluation")
        write_json(output / "ablation.json", {**json.loads(report.to_json()), "model": conditions})
        (output / "ablation.md").write_text(report.to_markdown(), encoding="utf-8")
        for variant in report.variants:
            (output / f"{variant.variant.name}.json").write_text(
                variant.report.to_json() + "\n", encoding="utf-8"
            )
        audit_results(report, output, secret)
        metadata["status"] = "completed"
    finally:
        if metadata["status"] == "running":
            metadata["status"] = "failed"
        metadata["finished_at"] = datetime.now(UTC).isoformat()
        write_json(output / "metadata.json", metadata)
    progress(f"REPORT {output / 'ablation.md'}")


if __name__ == "__main__":
    asyncio.run(main())

"""Command-line entry point for configuring and running Mini-dsh."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from minidsh import __version__
from minidsh.adapters import (
    JsonlTraceWriter,
    MLXAdapter,
    ScriptedModel,
    build_provider_model,
)
from minidsh.adapters.credentials import (
    CredentialSelection,
    CredentialStore,
    default_credentials_path,
)
from minidsh.adapters.plugin_trust import trusted_plugin_ids
from minidsh.benchmark import (
    AblationReport,
    AblationRunner,
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkTask,
    HarnessVariant,
    TaskMetrics,
    VariantRegistry,
)
from minidsh.benchmark.formal import (
    FORMAL_VARIANTS,
    Checkpoint,
    StudyKey,
    frozen_tree_digest,
    paired_summary,
    schedule,
)
from minidsh.benchmark.harness_runner import command as harness_study_command
from minidsh.collaboration import build_multi_agent_runtime
from minidsh.commands.arguments import build_parser
from minidsh.commands.configuration import config_command as _config
from minidsh.commands.configuration import resolve_arguments
from minidsh.commands.models import ModelSelection as _ModelSelection
from minidsh.commands.models import is_free, model_arguments, profile_for, select_model
from minidsh.commands.reports import report_command as _report
from minidsh.commands.reports import trace_diff as _trace_diff
from minidsh.commands.reports import trace_metrics as _trace_metrics
from minidsh.commands.reports import trace_replay as _trace_replay
from minidsh.commands.work import apply_work_preset
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.config import ResolvedConfig
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import ModelPricing, RunStatus, ToolCall
from minidsh.evaluation import TraceRun
from minidsh.evaluation.inference import inference_from_trace
from minidsh.gui import GuiSession, serve_gui
from minidsh.interactive import InteractiveChat
from minidsh.ports import Model

_MINIMUM_PYTHON: Final = (3, 12)
_FORMAL_TASKS: Final = 26
_FORMAL_REPEATS: Final = 3

# Existing embedding/tests use these names; both point to the single implementation.
_build_parser = build_parser
_work_config = apply_work_preset
_resolve = resolve_arguments
_model_arguments = model_arguments
_profile_for = profile_for
_is_free = is_free


def _compose_runtime(
    args: argparse.Namespace,
    workspace: Path,
    model: Model,
    resolved: ResolvedConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> LocalRuntime:
    if not resolved.options.multi_agent_enabled:
        options = resolved.options
        if options.enabled_tools is not None:
            options = replace(
                options, enabled_tools=options.enabled_tools - {"delegate_sub_agents"}
            )
        return build_configured_runtime(
            workspace=workspace,
            model=model,
            options=options,
            budget=resolved.budget,
            include_extension_tools=(
                args.command == "work" and "enabled_tools" not in resolved.sources
            ),
            trusted_plugins=trusted_plugin_ids(
                environ or os.environ,
                tuple(getattr(args, "trust_plugin", [])),
                read_global=bool(options.plugin_specs),
            ),
        )
    if resolved.options.plugin_specs or resolved.options.service_selection:
        raise ValueError("configured plugin composition currently requires single-agent mode")
    script = getattr(args, "subagent_script", None)
    if isinstance(model, ScriptedModel) and script is None:
        raise ValueError("scripted Multi-Agent requires --subagent-script")

    def factory() -> Model:
        if script is not None:
            return ScriptedModel.from_file(Path(script))
        selected = _select_model(args, resolved, environ=environ)
        if selected.model is None:
            raise ValueError("Sub Agent model adapter is unavailable")
        return selected.model

    return build_multi_agent_runtime(
        workspace=workspace,
        model=model,
        subagent_model_factory=factory,
        options=resolved.options,
        budget=resolved.budget,
        subagent_pricing=ModelPricing.free() if script is not None else resolved.options.pricing,
    )


def _doctor() -> int:
    python_ok = sys.version_info >= _MINIMUM_PYTHON
    uv_path = shutil.which("uv")
    git_path = shutil.which("git")
    healthy = python_ok and uv_path is not None and git_path is not None
    report = {
        "git": git_path,
        "mini_dsh_version": __version__,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_ok": python_ok,
        "status": "ok" if healthy else "error",
        "uv": uv_path,
    }
    sys.stdout.write(f"{json.dumps(report, indent=2, sort_keys=True)}\n")
    return 0 if healthy else 1


def _select_model(
    args: argparse.Namespace, resolved: ResolvedConfig, *, environ: Mapping[str, str] | None = None
) -> _ModelSelection:
    """Bind host construction capabilities at the CLI composition point."""
    return select_model(
        args,
        resolved,
        environ=environ,
        provider_factory=build_provider_model,
        credentials_path=default_credentials_path,
    )


def task_options(base: RuntimeOptions, task: BenchmarkTask) -> RuntimeOptions:
    """Overlay per-task isolation on top of the resolved CLI configuration."""
    isolation = task.isolation
    commands = set(base.allowed_commands) | set(isolation.allowed_commands)
    denied = set(base.denied_tools) | set(isolation.denied_tools)
    for command in (task.test_argv, task.build_argv):
        if command:
            commands.add(Path(command[0]).name)
    backend = base.sandbox_backend if base.sandbox_backend != "local" else isolation.sandbox_backend
    return replace(
        base,
        allow_write=base.allow_write or isolation.allow_write,
        allow_shell=base.allow_shell or isolation.allow_shell,
        allowed_commands=frozenset(commands),
        denied_tools=frozenset(denied),
        sandbox_backend=backend,
        network=base.network if base.network.enabled else isolation.network,
        test_command=task.test_argv,
        build_command=task.build_argv,
    )


def _write_outputs(report: BenchmarkReport, output: Path, markdown: Path | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_json() + "\n", encoding="utf-8")
    jsonl = output.parent / f"{output.stem}.jsonl"
    jsonl.write_text(report.to_jsonl(), encoding="utf-8")
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(report.to_markdown(), encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    workspace = await asyncio.to_thread(Path(args.workspace).resolve, strict=True)
    resolved = await asyncio.to_thread(_resolve, args, workspace)
    selection = await asyncio.to_thread(_select_model, args, resolved)
    model = selection.model
    if model is None:
        if selection.script is None:
            msg = "--script is required for the scripted provider"
            raise ValueError(msg)
        model = ScriptedModel.from_file(selection.script)
    model_load_ms = await model.prepare() if isinstance(model, MLXAdapter) else None
    run_id = uuid.uuid4().hex
    trace_path = (
        Path(args.trace)
        if args.trace is not None
        else workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
    )
    runtime = _compose_runtime(
        args,
        workspace,
        model,
        replace(resolved, options=replace(resolved.options, pricing=selection.pricing)),
    )
    runtime.events.subscribe("*", JsonlTraceWriter(trace_path))
    result = await runtime.agent.run(str(args.task), run_id=run_id)
    diff = await runtime.tools.execute(
        ToolCall("final-diff", "git", {"action": "diff", "paths": []}),
        run_id,
        result.steps,
    )
    diff_text = ""
    if isinstance(diff.result, dict):
        raw_stdout = diff.result.get("stdout")
        if isinstance(raw_stdout, str):
            diff_text = raw_stdout
    if runtime.close is not None:
        await runtime.close()
    inference = inference_from_trace(TraceRun.from_file(trace_path))
    output = {
        "answer": result.answer,
        "cost_usd": result.cost_usd,
        "diff": diff_text,
        "duration_ms": round(result.duration_ms, 3),
        "failure_reason": result.failure_reason,
        "latency": result.latency.to_json(),
        "inference": inference,
        "model": selection.describe(),
        "model_load_ms": model_load_ms,
        "run_id": result.run_id,
        "status": result.status.value,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "tokens": result.token_usage.total_tokens,
        "cached_input_tokens": result.token_usage.cached_input_tokens,
        "trace": str(trace_path),
    }
    sys.stdout.write(f"{json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True)}\n")
    return 0 if result.status is RunStatus.FINISHED else 1


def _benchmark_factory(
    base: RuntimeOptions,
    selection: _ModelSelection,
) -> Callable[[Path, BenchmarkTask], LocalRuntime]:
    options = replace(base, pricing=selection.pricing)

    def factory(workspace: Path, task: BenchmarkTask) -> LocalRuntime:
        model: Model
        if selection.model is not None:
            model = selection.model
        else:
            script = task.response_script or selection.script
            if script is None:
                msg = (
                    f"benchmark task {task.task_id} has no response_script and no --script "
                    "was given; either supply a script or select a real provider"
                )
                raise ValueError(msg)
            model = ScriptedModel.from_file(script)
        return build_local_runtime(
            workspace=workspace,
            model=model,
            options=task_options(options, task),
            budget=task.budget,
        )

    return factory


def _require_verification(args: argparse.Namespace) -> frozenset[str]:
    allowlist = frozenset(str(item) for item in args.verify_command)
    if not allowlist:
        msg = "at least one explicit --verify-command is required"
        raise ValueError(msg)
    return allowlist


async def _benchmark(args: argparse.Namespace) -> int:
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, Path(args.manifest))
    resolved = await asyncio.to_thread(_resolve, args, Path.cwd())
    selection = await asyncio.to_thread(_select_model, args, resolved)
    if isinstance(selection.model, MLXAdapter):
        await selection.model.prepare()
    verification_allowlist = _require_verification(args)
    output = await asyncio.to_thread(Path(args.output).resolve)
    runner = BenchmarkRunner(
        _benchmark_factory(resolved.options, selection),
        output.parent / f"{output.stem}-artifacts",
        verification_allowlist=verification_allowlist,
        repeats=int(args.repeat),
        model=selection.describe(),
    )
    report = await runner.run(manifest)
    markdown = Path(args.markdown) if args.markdown is not None else output.with_suffix(".md")
    await asyncio.to_thread(_write_outputs, report, output, markdown)
    sys.stdout.write(f"{report.to_markdown()}\n")
    return 0 if report.aggregate()["success_rate"] == 1.0 else 1


def _variant_options(base: RuntimeOptions, variant: HarnessVariant) -> RuntimeOptions:
    return replace(
        base,
        context_strategy=variant.context_strategy,
        reflection_enabled=variant.reflection_enabled,
        detailed_tool_feedback=variant.detailed_tool_feedback,
        tool_output_limit=variant.tool_output_limit,
        artifact_offload=variant.artifact_offload,
        tool_max_retries=variant.max_retries,
        local_first_enabled=variant.local_first_enabled,
        progressive_tools_enabled=variant.progressive_tools_enabled,
        normalize_tool_results=variant.normalize_tool_results,
        error_recovery_enabled=variant.error_recovery_enabled,
    )


async def _ablation(args: argparse.Namespace) -> int:
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, Path(args.manifest))
    resolved = await asyncio.to_thread(_resolve, args, Path.cwd())
    selection = await asyncio.to_thread(_select_model, args, resolved)
    if isinstance(selection.model, MLXAdapter):
        await selection.model.prepare()
    verification_allowlist = _require_verification(args)
    variants = VariantRegistry().get(str(args.suite))
    output = await asyncio.to_thread(Path(args.output).resolve)

    def factory(variant: HarnessVariant) -> BenchmarkRunner:
        base = _variant_options(resolved.options, variant)
        return BenchmarkRunner(
            _benchmark_factory(base, selection),
            output.parent / f"{output.stem}-{variant.name}",
            verification_allowlist=verification_allowlist,
            repeats=int(args.repeat),
            model=selection.describe(),
        )

    report: AblationReport = await AblationRunner(factory).run(manifest, variants)
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    payload = {
        "manifest": report.manifest_name,
        "model": selection.describe(),
        "variants": json.loads(report.to_json())["variants"],
        "pareto": [item.to_json() for item in report.pareto()],
    }
    await asyncio.to_thread(
        output.write_text, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    jsonl = output.parent / f"{output.stem}.jsonl"
    await asyncio.to_thread(jsonl.write_text, report.to_jsonl(), encoding="utf-8")
    markdown = output.with_suffix(".md")
    await asyncio.to_thread(markdown.write_text, report.to_markdown(), encoding="utf-8")
    sys.stdout.write(f"{report.to_markdown()}\n")
    return (
        0 if all(item.report.aggregate()["success_rate"] == 1.0 for item in report.variants) else 1
    )


async def _matrix(args: argparse.Namespace) -> int:
    """Run four local model and harness cells sequentially on fixed fixtures."""
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, Path(args.manifest))
    resolved = await asyncio.to_thread(_resolve, args, Path.cwd())
    allowlist = _require_verification(args)
    output = await asyncio.to_thread(Path(args.output).resolve)
    cells: list[dict[str, object]] = []
    for size, raw_path in (("4b", args.model_4b), ("8b", args.model_8b)):
        path = await asyncio.to_thread(
            lambda value: Path(value).expanduser().resolve(strict=True), raw_path
        )
        if (
            not await asyncio.to_thread(path.is_dir)
            or re.fullmatch(r"[0-9a-f]{40}", path.name) is None
        ):
            raise ValueError(
                "matrix models must be local pinned snapshot directories (40-char revision)"
            )
        model = MLXAdapter(str(path), input_budget=resolved.options.context_input_budget)
        load_ms = await model.prepare()
        selection = _ModelSelection(model, f"qwen3-{size}-4bit", None, ModelPricing.free(), None)
        for variant in ("baseline", "optimized"):
            options = replace(
                resolved.options,
                context_strategy="full" if variant == "baseline" else "budget",
                local_first_enabled=variant == "optimized",
                progressive_tools_enabled=variant == "optimized",
                normalize_tool_results=variant == "optimized",
            )
            runner = BenchmarkRunner(
                _benchmark_factory(options, selection),
                output.parent / f"{output.stem}-{size}-{variant}",
                verification_allowlist=allowlist,
                repeats=int(args.repeat),
                model={**selection.describe(), "load_ms": load_ms},
            )
            report = await runner.run(manifest)
            cells.append(
                {"model": size, "harness": variant, "report": json.loads(report.to_json())}
            )
        await model.release()
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text,
        json.dumps(
            {"schema_version": "1.0", "manifest": manifest.name, "cells": cells},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    lines = [
        "| Model | Harness | Success | Tokens | Steps | Time (ms) |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for cell in cells:
        report_data = cell["report"]
        assert isinstance(report_data, dict)
        aggregate = report_data["aggregate"]
        assert isinstance(aggregate, dict)
        lines.append(
            f"| {cell['model']} | {cell['harness']} | {aggregate['success_rate']:.0%} | "
            f"{aggregate['average_tokens']:.0f} | {aggregate['average_steps']:.1f} | "
            f"{aggregate['average_latency_ms']:.0f} |"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _formal_options(variant: HarnessVariant) -> RuntimeOptions:
    """Apply the frozen policy without ambient config or environment overrides."""
    tools = {"read_file", "search", "patch_file", "write_file", "test"}
    if variant.progressive_tools_enabled:
        tools.update({"list_files", "search_code", "find_symbol"})
    return RuntimeOptions(
        context_strategy=variant.context_strategy,
        context_input_budget=8_192,
        local_first_enabled=variant.local_first_enabled,
        error_recovery_enabled=variant.error_recovery_enabled,
        progressive_tools_enabled=variant.progressive_tools_enabled,
        normalize_tool_results=True,
        allow_write=True,
        allow_shell=True,
        allowed_commands=frozenset({"python"}),
        enabled_tools=frozenset(tools),
        tool_output_limit=variant.tool_output_limit,
        artifact_offload=True,
        tool_max_retries=1,
    )


def _formal_protocol_hash(
    manifest_path: Path,
    model_paths: dict[str, Path],
    repeats: int,
) -> str:
    source = Path(__file__).resolve().parent
    payload = {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "code": frozen_tree_digest(source),
        "uv_lock_sha256": _formal_lock_hash(),
        "generator_sha256": hashlib.sha256(
            (source.parent.parent / "benchmarks" / "build_formal.py").read_bytes()
        ).hexdigest(),
        "models": {name: str(path) for name, path in model_paths.items()},
        "repeats": repeats,
        "variants": [asdict(item) for item in FORMAL_VARIANTS],
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _formal_lock_hash() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()


async def _formal_study(args: argparse.Namespace) -> int:  # noqa: PLR0912, PLR0915
    """Run and checkpoint each measured local task, then write complete reports."""
    manifest_path = await asyncio.to_thread(Path(args.manifest).resolve, strict=True)
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, manifest_path)
    if manifest.name != "mini-dsh-local-formal-v1" or len(manifest.tasks) != _FORMAL_TASKS:
        raise ValueError("formal-study requires the frozen 26-task local formal manifest")
    repeats = int(args.repeat)
    if repeats != _FORMAL_REPEATS:
        raise ValueError("formal-study v1 requires exactly three repeats")
    model_paths: dict[str, Path] = {}
    for name, raw in (("4b", args.model_4b), ("8b", args.model_8b)):
        model_paths[name] = await asyncio.to_thread(
            lambda value: Path(value).expanduser().resolve(strict=True), raw
        )
    for path in model_paths.values():
        if not path.is_dir() or re.fullmatch(r"[0-9a-f]{40}", path.name) is None:
            raise ValueError("formal-study models must be pinned 40-character MLX snapshots")
    BenchmarkRunner._validate_manifest(manifest)
    output = await asyncio.to_thread(Path(args.output_dir).resolve)
    protocol_hash = _formal_protocol_hash(manifest_path, model_paths, repeats)
    checkpoint = Checkpoint(output / "results.jsonl", protocol_hash)
    protocol_path = output / "protocol.json"
    protocol = {
        "schema_version": "formal-study-1.0",
        "protocol_hash": protocol_hash,
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "uv_lock_sha256": await asyncio.to_thread(_formal_lock_hash),
        "code_digest": await asyncio.to_thread(
            lambda: frozen_tree_digest(Path(__file__).resolve().parent)
        ),
        "model_snapshots": {name: str(path) for name, path in model_paths.items()},
        "variants": [asdict(item) for item in FORMAL_VARIANTS],
        "repeats": repeats,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sampling": "greedy; thinking disabled; output cap 1024",
    }
    if protocol_path.is_file():
        if json.loads(protocol_path.read_text(encoding="utf-8")) != protocol:
            raise ValueError("existing formal protocol metadata differs")
    else:
        output.mkdir(parents=True, exist_ok=True)
        protocol_path.write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
    variants = {item.name: item for item in FORMAL_VARIANTS}
    tasks = {task.task_id: task for task in manifest.tasks}
    all_runs: list[StudyKey] = []
    if args.phase in {"matrix", "all"}:
        all_runs.extend(
            schedule(
                tuple(tasks),
                repeats=3,
                models=("4b", "8b"),
                variants=("bounded_20k", "loop_guard_recovery"),
                study="matrix",
            )
        )
    if args.phase in {"ablation", "all"}:
        all_runs.extend(
            schedule(
                tuple(tasks),
                repeats=3,
                models=("8b",),
                variants=tuple(variants),
                study="ablation",
            )
        )
    active_model: MLXAdapter | None = None
    active_label: str | None = None
    active_repeat: int | None = None
    active_study: str | None = None
    completed = 0
    try:
        for key in all_runs:
            if checkpoint.get(key) is not None:
                completed += 1
                continue
            if (
                active_label != key.model
                or active_repeat != key.repeat
                or active_study != key.study
            ):
                if active_model is not None:
                    await active_model.release()
                active_model = MLXAdapter(str(model_paths[key.model]), input_budget=8_192)
                load_ms = await active_model.prepare()
                active_label = key.model
                active_repeat = key.repeat
                active_study = key.study
                sys.stdout.write(f"loaded {key.model} in {load_ms:.0f} ms\n")
                sys.stdout.flush()
            assert active_model is not None
            variant = variants[key.variant]
            selection = _ModelSelection(
                active_model, f"qwen3-{key.model}-4bit", None, ModelPricing.free(), None
            )
            runner = BenchmarkRunner(
                _benchmark_factory(_formal_options(variant), selection),
                output / "runs" / key.study / key.model / key.variant,
                verification_allowlist=frozenset({"python"}),
                model=selection.describe(),
            )
            result = await runner.run_one(tasks[key.task_id], key.repeat)
            checkpoint.append(key, result)
            completed += 1
            sys.stdout.write(
                f"[{completed}/{len(all_runs)}] {key.slug()} "
                f"success={result.success} tokens={result.tokens} "
                f"wall_ms={result.latency_ms:.0f}\n"
            )
            sys.stdout.flush()
    finally:
        if active_model is not None:
            await active_model.release()
    _write_formal_report(output, checkpoint, all_runs, manifest, protocol_hash, model_paths)
    return 0


def _write_formal_report(  # noqa: PLR0913, PLR0917 - fixed study report inputs
    output: Path,
    checkpoint: Checkpoint,
    runs: list[StudyKey],
    manifest: BenchmarkManifest,
    protocol_hash: str,
    model_paths: dict[str, Path],
) -> None:
    """Publish aggregates only for complete cells; retain every raw attempt."""
    grouped: dict[tuple[str, str, str], list[TaskMetrics]] = {}
    for key in runs:
        if checkpoint.get(key) is None:
            raise ValueError("formal report requested before all runs completed")
    for key, result in checkpoint.items():
        grouped.setdefault((key.study, key.model, key.variant), []).append(result)
    cells = []
    for (study, model, variant), results in sorted(grouped.items()):
        if len(results) != len(manifest.tasks) * _FORMAL_REPEATS:
            continue
        report = BenchmarkReport(manifest.name, manifest.schema_version, tuple(results))
        by_category: dict[str, dict[str, float | int]] = {}
        for category in sorted({item.category for item in results}):
            subset = [item for item in results if item.category == category]
            by_category[category] = {
                "runs": len(subset),
                "success_rate": sum(item.success for item in subset) / len(subset),
                "mean_tokens": statistics.fmean(item.tokens for item in subset),
            }
        successful = [item for item in results if item.success]
        cells.append(
            {
                "study": study,
                "model": model,
                "variant": variant,
                "aggregate": report.aggregate(),
                "median_tokens": statistics.median(item.tokens for item in results),
                "median_wall_ms": statistics.median(item.latency_ms for item in results),
                "median_steps": statistics.median(item.steps for item in results),
                "median_tool_calls": statistics.median(item.tool_calls for item in results),
                "mean_prompt_tokens": statistics.fmean(item.input_tokens for item in results),
                "mean_output_tokens": statistics.fmean(item.output_tokens for item in results),
                "mean_ttft_ms": statistics.fmean(
                    item.ttft_ms for item in results if item.ttft_ms is not None
                )
                if any(item.ttft_ms is not None for item in results)
                else None,
                "mean_prefill_tokens_per_second": statistics.fmean(
                    item.prefill_tokens_per_second
                    for item in results
                    if item.prefill_tokens_per_second is not None
                )
                if any(item.prefill_tokens_per_second is not None for item in results)
                else None,
                "mean_decode_tokens_per_second": statistics.fmean(
                    item.decode_tokens_per_second
                    for item in results
                    if item.decode_tokens_per_second is not None
                )
                if any(item.decode_tokens_per_second is not None for item in results)
                else None,
                "loop_guard_triggers": sum(item.loop_guard_triggers for item in results),
                "tool_retry_count": sum(item.retries for item in results),
                "model_format_retry_count": sum(item.model_format_retries for item in results),
                "peak_process_gb": max(
                    (item.process_peak_gb for item in results if item.process_peak_gb is not None),
                    default=None,
                ),
                "failure_types": report.failure_type_counts(),
                "by_category": by_category,
                "success_only": {
                    "runs": len(successful),
                    "mean_tokens": statistics.fmean(item.tokens for item in successful)
                    if successful
                    else None,
                    "mean_wall_ms": statistics.fmean(item.latency_ms for item in successful)
                    if successful
                    else None,
                },
            }
        )

    def compare(
        left_cell: tuple[str, str, str], right_cell: tuple[str, str, str]
    ) -> dict[str, Any] | None:
        pairs: dict[str, list[tuple[TaskMetrics, TaskMetrics]]] = {}
        for task in manifest.tasks:
            pairs[task.task_id] = []
            for repeat in range(_FORMAL_REPEATS):
                left = checkpoint.get(StudyKey(*left_cell, task.task_id, repeat))
                right = checkpoint.get(StudyKey(*right_cell, task.task_id, repeat))
                if left is not None and right is not None:
                    pairs[task.task_id].append((left, right))
        return (
            paired_summary(pairs)
            if all(len(items) == _FORMAL_REPEATS for items in pairs.values())
            else None
        )

    comparisons = {
        "8b_optimized_vs_baseline": compare(
            ("matrix", "8b", "bounded_20k"),
            ("matrix", "8b", "loop_guard_recovery"),
        ),
        "4b_optimized_vs_baseline": compare(
            ("matrix", "4b", "bounded_20k"),
            ("matrix", "4b", "loop_guard_recovery"),
        ),
        "4b_optimized_vs_8b_baseline": compare(
            ("matrix", "8b", "bounded_20k"),
            ("matrix", "4b", "loop_guard_recovery"),
        ),
    }
    comparisons.update(
        {
            f"ablation_{before.name}_to_{after.name}": compare(
                ("ablation", "8b", before.name), ("ablation", "8b", after.name)
            )
            for before, after in pairwise(FORMAL_VARIANTS)
        }
    )
    payload = {
        "schema_version": "formal-study-1.0",
        "protocol_hash": protocol_hash,
        "manifest": manifest.name,
        "model_snapshots": {name: str(path) for name, path in model_paths.items()},
        "tasks": len(manifest.tasks),
        "repeats": 3,
        "cells": cells,
        "paired_comparisons": comparisons,
        "limitations": [
            "Three greedy repeats measure stability; task is the bootstrap unit.",
            "The local workspace sandbox is not an OS isolation boundary.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "report.json.tmp"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output / "report.json")


def _chat(args: argparse.Namespace) -> int:  # noqa: PLR0915 - explicit interactive composition
    if getattr(args, "session", None) and args.resume:
        raise ValueError("choose either --session or --resume")
    workspace = Path(args.workspace).resolve(strict=True)
    if args.script is not None and args.model_profile is None:
        args.provider = "scripted"
        # An explicit offline script must also win over environment profiles.
        args.model_profile = None
    resolved = _resolve(args, workspace)
    args = _model_arguments(args, resolved)
    if args.script is None and (
        args.provider == "openai-compatible"
        and not args.model_profile
        and not os.environ.get("MINIDSH_MODEL_PROFILE")
        and not args.endpoint
        and not os.environ.get("MINIDSH_MODEL_ENDPOINT")
    ):
        args.model_profile = "deepseek"
    if args.command == "work":
        resolved = _work_config(args, resolved)
    else:
        interactive_defaults: dict[str, Any] = {"streaming_enabled": True, "approval_mode": "ask"}
        resolved = replace(
            resolved,
            options=replace(
                resolved.options,
                **{
                    key: value
                    for key, value in interactive_defaults.items()
                    if key not in resolved.sources
                },
            ),
        )
    profile = _profile_for(args) if args.script is None else None
    credential_store = (
        CredentialStore(workspace, default_credentials_path()) if profile is not None else None
    )
    credential = (
        credential_store.resolve(profile.api_key_env, os.environ)
        if credential_store is not None and profile is not None
        else CredentialSelection(None, "not used")
    )

    budget_state = {"value": resolved.budget}

    def configured_factory(options: RuntimeOptions, api_key: str | None = None) -> LocalRuntime:
        environment: Mapping[str, str] = os.environ
        selected_key = api_key if api_key is not None else credential.value
        if selected_key is not None:
            if profile is None:
                raise ValueError("offline models do not use an API Key")
            environment = {**os.environ, profile.api_key_env: selected_key}
        if args.script is not None and args.model_profile is None:
            model: Model = ScriptedModel.from_file(args.script)
            pricing = resolved.options.pricing
        else:
            selection = _select_model(args, resolved, environ=environment)
            if selection.model is None:
                raise ValueError("--script is required for the scripted provider")
            model = selection.model
            pricing = selection.pricing
        return _compose_runtime(
            args,
            workspace,
            model,
            replace(
                resolved,
                budget=budget_state["value"],
                options=replace(options, pricing=pricing),
            ),
            environ=environment,
        )

    def factory() -> LocalRuntime:
        return configured_factory(resolved.options)

    async def save_key(key: str) -> Path:
        if credential_store is None or profile is None:
            raise ValueError("offline models do not use an API Key")
        return await credential_store.save_workspace(profile.api_key_env, key)

    label = (
        "scripted"
        if args.script is not None
        else (
            args.model
            or os.environ.get("MINIDSH_MODEL")
            or args.model_profile
            or os.environ.get("MINIDSH_MODEL_PROFILE")
            or "openai-compatible"
        )
    )
    priced = not _is_free(resolved.options.pricing)
    if args.command == "gui":
        profile = _profile_for(args) if args.script is None else None
        priced = priced or (profile is not None and profile.pricing is not None)
        session = GuiSession(
            workspace=workspace,
            runtime_factory=factory,
            model_label=str(label),
            options=resolved.options,
            cost_reportable=priced,
            settings_factory=configured_factory,
            credentials_editable=profile is not None,
            api_key_available=credential.value is not None,
        )

        async def browser_session() -> None:
            if args.resume is not None:
                await session.action("resume", {"run_id": args.resume})
            elif getattr(args, "session", None):
                await session.action("session", {"session_id": args.session})
            await serve_gui(session, port=args.port, open_browser=not args.no_open)

        with suppress(KeyboardInterrupt):
            asyncio.run(browser_session())
        return 0
    chat_ui = InteractiveChat(
        workspace=workspace,
        runtime_factory=factory,
        model_label=str(label),
        options=resolved.options,
        cost_reportable=priced,
        resume=args.resume,
        budget=resolved.budget,
        budget_applier=lambda budget: budget_state.__setitem__("value", budget),
        credential_factory=(
            (lambda key: configured_factory(resolved.options, key))
            if args.script is None and args.provider == "openai-compatible"
            else None
        ),
        credential_saver=save_key if credential_store is not None else None,
        credential_source=credential.source if profile is not None else None,
        credential_required=(
            profile.api_key_env
            if profile is not None and profile.requires_api_key and credential.value is None
            else None
        ),
    )
    if getattr(args, "session", None):
        chat_ui.select_session(args.session)
    return chat_ui.run()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Mini-dsh command-line interface."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (
        arguments[0].startswith("--")
        and arguments[0]
        not in {
            "--help",
            "--version",
        }
    ):
        arguments.insert(0, "work")
    args = _build_parser().parse_args(arguments)
    if args.command == "doctor":
        raise SystemExit(_doctor())
    handlers: Mapping[str, Any] = {
        "run": _run,
        "benchmark": _benchmark,
        "ablation": _ablation,
        "matrix": _matrix,
        "formal-study": _formal_study,
        "harness-study": harness_study_command,
        "report": _report,
        "config": _config,
    }
    try:
        if args.command in {"chat", "gui", "work"}:
            exit_code = _chat(args)
        elif args.command == "config":
            exit_code = _config(args)
        elif args.command == "report":
            exit_code = _report(args)
        elif args.command == "trace":
            trace_handlers = {
                "replay": _trace_replay,
                "diff": _trace_diff,
                "metrics": _trace_metrics,
            }
            exit_code = trace_handlers[args.trace_command](args)
        else:
            handler = handlers.get(str(args.command))
            if handler is None:
                raise AssertionError(f"unhandled command: {args.command}")
            exit_code = asyncio.run(handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"mini-dsh: {exc}\n")
        exit_code = 2
    raise SystemExit(exit_code)

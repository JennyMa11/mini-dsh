"""Command-line entry point for configuring and running Mini-dsh."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shlex
import shutil
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from minidsh import __version__
from minidsh.adapters import (
    BUILTIN_PROFILES,
    JsonlTraceWriter,
    ModelAdapterError,
    ModelProfile,
    ScriptedModel,
    build_provider_model,
    resolve_profile,
)
from minidsh.benchmark import (
    AblationReport,
    AblationRunner,
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkTask,
    HarnessVariant,
    VariantRegistry,
)
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.config import DEFAULT_CONFIG_NAME, ResolvedConfig, resolve_config
from minidsh.domain import FailureCategory, ModelPricing, RunStatus, ToolCall
from minidsh.evaluation import (
    MetricPoint,
    TraceRun,
    build_replay,
    diff_traces,
    pareto,
)
from minidsh.evaluation.diff import to_markdown as diff_markdown
from minidsh.evaluation.replay import to_markdown as replay_markdown
from minidsh.ports import Model

_MINIMUM_PYTHON: Final = (3, 12)
_CONTEXT_STRATEGIES: Final = (
    "full",
    "sliding",
    "summary",
    "retrieval",
    "retrieval_summary",
    "symbol",
)
_PROVIDERS: Final = ("scripted", "openai-compatible")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-dsh",
        description="A lightweight coding-agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate the local runtime environment")

    run = subparsers.add_parser("run", help="run one coding-agent task")
    _add_common_options(run)
    _add_model_options(run)
    run.add_argument("task", help="task given to the coding agent")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--trace", type=Path, help="JSONL trace path")

    benchmark = subparsers.add_parser("benchmark", help="run a benchmark manifest")
    _add_common_options(benchmark)
    _add_model_options(benchmark)
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--output", type=Path, default=Path("benchmark-results/report.json"))
    benchmark.add_argument("--markdown", type=Path, help="optional Markdown report path")
    benchmark.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every task this many times to expose run-to-run spread",
    )
    _add_verification_option(benchmark)

    ablation = subparsers.add_parser("ablation", help="run a single-variable ablation suite")
    _add_common_options(ablation)
    _add_model_options(ablation)
    ablation.add_argument("manifest", type=Path)
    ablation.add_argument("--output", type=Path, default=Path("benchmark-results/ablation.json"))
    ablation.add_argument(
        "--suite",
        default="standard",
        help="variant suite: standard, context, tool_output, permission",
    )
    ablation.add_argument("--repeat", type=int, default=1)
    _add_verification_option(ablation)

    report = subparsers.add_parser("report", help="render a saved report with trade-off analysis")
    report.add_argument("path", type=Path, help="a benchmark or ablation JSON report")
    report.add_argument("--markdown", type=Path, help="write Markdown here instead of stdout")

    trace = subparsers.add_parser("trace", help="replay or diff recorded JSONL traces")
    trace_commands = trace.add_subparsers(dest="trace_command", required=True)
    trace_replay = trace_commands.add_parser("replay", help="reconstruct a run step by step")
    trace_replay.add_argument("path", type=Path)
    trace_replay.add_argument("--json", action="store_true", dest="as_json")
    trace_replay.add_argument("--preview-chars", type=int, default=120)
    trace_diff = trace_commands.add_parser("diff", help="compare two traces of the same task")
    trace_diff.add_argument("left", type=Path)
    trace_diff.add_argument("right", type=Path)
    trace_diff.add_argument("--left-label")
    trace_diff.add_argument("--right-label")
    trace_diff.add_argument("--json", action="store_true", dest="as_json")

    config = subparsers.add_parser("config", help="print the effective layered configuration")
    _add_common_options(config)
    config.add_argument("--workspace", type=Path, default=Path.cwd())
    return parser


def _add_verification_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="allow one success-criterion executable; repeat as needed",
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help=f"configuration file; defaults to ./{DEFAULT_CONFIG_NAME} when present",
    )
    parser.add_argument("--context", choices=_CONTEXT_STRATEGIES)
    parser.add_argument("--allow-write", action="store_true", default=None)
    parser.add_argument("--allow-shell", action="store_true", default=None)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="HOST[:PORT]",
        help="authorize one network destination; repeat as needed",
    )
    parser.add_argument("--sandbox-backend", choices=("local", "docker"))
    parser.add_argument("--docker-image")
    parser.add_argument(
        "--allow-command",
        action="append",
        default=None,
        metavar="EXECUTABLE",
        help="allow one executable for shell/test/build tools; repeat as needed",
    )
    parser.add_argument(
        "--deny-tool",
        action="append",
        default=None,
        metavar="TOOL",
        help="deny one tool even when its capability is granted; repeat as needed",
    )
    parser.add_argument(
        "--test-command", metavar="COMMAND", help="fixed test command, shell-split once"
    )
    parser.add_argument(
        "--build-command", metavar="COMMAND", help="fixed build command, shell-split once"
    )
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--tool-output-limit", type=int)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument(
        "--cached-input-usd-per-million",
        type=float,
        help="rate for prompt-cache hits; defaults to the uncached input rate",
    )


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=_PROVIDERS, default="scripted")
    parser.add_argument(
        "--model-profile",
        help=f"named provider profile: {', '.join(sorted(BUILTIN_PROFILES))}",
    )
    parser.add_argument(
        "--script", type=Path, help="JSON response script; required by the scripted provider"
    )
    parser.add_argument("--endpoint", help="Chat Completions endpoint; overrides the profile")
    parser.add_argument("--model", help="model name; overrides the profile")
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="environment variable containing the provider API key; defaults to the profile's",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="attempts per model call before a transient provider error is fatal",
    )
    parser.add_argument(
        "--allow-unpriced",
        action="store_true",
        help="permit a real provider with no declared rate card; cost is then not reportable",
    )


def _discover_config_path(explicit: Path | None, workspace: Path) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = workspace / DEFAULT_CONFIG_NAME
    return candidate if candidate.is_file() else None


def _network_rules(hosts: Sequence[str] | None) -> list[dict[str, Any]] | None:
    if hosts is None:
        return None
    rules: list[dict[str, Any]] = []
    for entry in hosts:
        host, _, port = entry.partition(":")
        rule: dict[str, Any] = {"host": host, "protocol": "tcp"}
        if port:
            rule["ports"] = [int(port)]
        rules.append(rule)
    return rules


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "context_strategy": getattr(args, "context", None),
        "allow_write": getattr(args, "allow_write", None),
        "allow_shell": getattr(args, "allow_shell", None),
        "sandbox_backend": getattr(args, "sandbox_backend", None),
        "docker_image": getattr(args, "docker_image", None),
        "max_steps": getattr(args, "max_steps", None),
        "max_tokens": getattr(args, "max_tokens", None),
        "max_tool_calls": getattr(args, "max_tool_calls", None),
        "max_duration_seconds": getattr(args, "max_seconds", None),
        "tool_output_limit": getattr(args, "tool_output_limit", None),
        "input_usd_per_million": getattr(args, "input_usd_per_million", None),
        "output_usd_per_million": getattr(args, "output_usd_per_million", None),
        "cached_input_usd_per_million": getattr(args, "cached_input_usd_per_million", None),
    }
    commands = getattr(args, "allow_command", None)
    if commands is not None:
        overrides["allowed_commands"] = list(dict.fromkeys(["git", *commands]))
    denied_tools = getattr(args, "deny_tool", None)
    if denied_tools is not None:
        overrides["denied_tools"] = list(dict.fromkeys(denied_tools))
    rules = _network_rules(getattr(args, "allow_host", None))
    if rules is not None:
        overrides["network_rules"] = rules
    test_command = getattr(args, "test_command", None)
    if test_command:
        overrides["test_command"] = shlex.split(str(test_command))
    build_command = getattr(args, "build_command", None)
    if build_command:
        overrides["build_command"] = shlex.split(str(build_command))
    return overrides


def _resolve(args: argparse.Namespace, workspace: Path) -> ResolvedConfig:
    config_path = _discover_config_path(getattr(args, "config", None), workspace)
    return resolve_config(
        cli=_cli_overrides(args),
        environ=os.environ,
        config_path=config_path,
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


@dataclass(frozen=True, slots=True)
class _ModelSelection:
    """The model adapter plus everything a report needs to describe it."""

    model: Model | None
    label: str
    profile: ModelProfile | None
    pricing: ModelPricing
    script: Path | None

    @property
    def is_real_provider(self) -> bool:
        """Return whether a network provider will be called."""
        return self.profile is not None

    def describe(self) -> dict[str, Any]:
        """Return the provider facts recorded alongside results."""
        payload: dict[str, Any] = {
            "label": self.label,
            "pricing": {
                "input_usd_per_million": self.pricing.input_usd_per_million,
                "output_usd_per_million": self.pricing.output_usd_per_million,
                "cached_input_usd_per_million": self.pricing.cached_input_usd_per_million,
            },
        }
        if self.profile is not None:
            payload["profile"] = self.profile.describe()
        return payload


def _is_free(pricing: ModelPricing) -> bool:
    return pricing.input_usd_per_million == 0 and pricing.output_usd_per_million == 0


def _apply_model_overrides(profile: ModelProfile, args: argparse.Namespace) -> ModelProfile:
    """Let explicit flags win over the profile's pinned endpoint, model, and key var."""
    model = getattr(args, "model", None)
    endpoint = getattr(args, "endpoint", None)
    api_key_env = getattr(args, "api_key_env", None)
    if model is None and endpoint is None and api_key_env is None:
        return profile
    return replace(
        profile,
        model=str(model) if model else profile.model,
        endpoint=str(endpoint) if endpoint else profile.endpoint,
        api_key_env=str(api_key_env) if api_key_env else profile.api_key_env,
    )


def _profile_for(args: argparse.Namespace) -> ModelProfile | None:
    """Resolve the named profile, or synthesise one from explicit endpoint flags."""
    name = getattr(args, "model_profile", None) or os.environ.get("MINIDSH_MODEL_PROFILE")
    if name:
        return _apply_model_overrides(resolve_profile(str(name), environ=os.environ), args)
    if getattr(args, "provider", "scripted") != "openai-compatible":
        return None
    endpoint = getattr(args, "endpoint", None) or os.environ.get("MINIDSH_MODEL_ENDPOINT")
    model_name = getattr(args, "model", None) or os.environ.get("MINIDSH_MODEL")
    if not endpoint or not model_name:
        msg = (
            "openai-compatible requires --endpoint/MINIDSH_MODEL_ENDPOINT and "
            "--model/MINIDSH_MODEL, or a named --model-profile"
        )
        raise ModelAdapterError(msg, code="missing_endpoint")
    return ModelProfile(
        name="openai-compatible",
        endpoint=str(endpoint),
        model=str(model_name),
        api_key_env=str(getattr(args, "api_key_env", None) or "MINIDSH_API_KEY"),
        pricing=None,
        pricing_source="Explicit endpoint: declare the rate card on the command line.",
    )


def _select_model(args: argparse.Namespace, resolved: ResolvedConfig) -> _ModelSelection:
    """Build the model adapter and the rate card its results will be priced with."""
    profile = _profile_for(args)
    if profile is None:
        script = getattr(args, "script", None)
        return _ModelSelection(
            None,
            "scripted",
            None,
            resolved.options.pricing,
            script if isinstance(script, Path) else None,
        )

    # A zero rate card is indistinguishable from an undeclared one, so "declared" is
    # tracked explicitly: a profile that genuinely costs nothing per token (local
    # weights) must not be rejected as unpriced, while a paid provider must not
    # silently report $0.
    declared = not _is_free(resolved.options.pricing)
    pricing = resolved.options.pricing
    if profile.pricing is not None:
        pricing = resolved.options.pricing if declared else profile.pricing
        declared = True
    if not declared and not getattr(args, "allow_unpriced", False):
        msg = (
            f"profile {profile.name!r} has no declared rate card, so cost cannot be reported. "
            "Pass --input-usd-per-million and --output-usd-per-million (and optionally "
            "--cached-input-usd-per-million) from the provider's current pricing page, "
            "or pass --allow-unpriced to run without cost accounting."
        )
        raise ValueError(msg)
    model = build_provider_model(
        profile,
        environ=os.environ,
        temperature=float(getattr(args, "temperature", 0.0)),
        max_attempts=int(getattr(args, "max_attempts", 4)),
    )
    return _ModelSelection(model, profile.resolved_model(os.environ), profile, pricing, None)


def _task_options(base: RuntimeOptions, task: BenchmarkTask) -> RuntimeOptions:
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
    run_id = uuid.uuid4().hex
    trace_path = (
        Path(args.trace)
        if args.trace is not None
        else workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=replace(resolved.options, pricing=selection.pricing),
        budget=resolved.budget,
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
    output = {
        "answer": result.answer,
        "cost_usd": result.cost_usd,
        "diff": diff_text,
        "duration_ms": round(result.duration_ms, 3),
        "failure_reason": result.failure_reason,
        "latency": result.latency.to_json(),
        "model": selection.describe(),
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
            options=_task_options(options, task),
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
    verification_allowlist = _require_verification(args)
    output = await asyncio.to_thread(Path(args.output).resolve)
    runner = BenchmarkRunner(
        _benchmark_factory(resolved.options, selection),
        output.parent / f"{output.stem}-artifacts",
        verification_allowlist=verification_allowlist,
        repeats=int(args.repeat),
        model_label=selection.label if selection.is_real_provider else None,
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
    )


async def _ablation(args: argparse.Namespace) -> int:
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, Path(args.manifest))
    resolved = await asyncio.to_thread(_resolve, args, Path.cwd())
    selection = await asyncio.to_thread(_select_model, args, resolved)
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
            model_label=selection.label if selection.is_real_provider else None,
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


def _report(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read report: {path}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"report must be a JSON object: {path}"
        raise ValueError(msg)
    rendered = (
        _render_ablation_report(payload)
        if "variants" in payload
        else _render_benchmark_report(payload)
    )
    if args.markdown is not None:
        destination = Path(args.markdown)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"wrote {destination}\n")
    else:
        sys.stdout.write(rendered)
    return 0


def _render_ablation_report(payload: dict[str, Any]) -> str:
    variants = payload.get("variants")
    if not isinstance(variants, list):
        msg = "ablation report has no variants"
        raise ValueError(msg)
    lines = [f"# Ablation report: {payload.get('manifest', 'unknown')}", ""]
    model = payload.get("model")
    if isinstance(model, dict):
        lines += [f"- Model: `{model.get('label')}`", ""]
    lines += [
        "| Variant | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    points: list[MetricPoint] = []
    for item in variants:
        if not isinstance(item, dict):
            continue
        configuration = item.get("configuration", {})
        aggregate = item.get("aggregate", {})
        name = str(configuration.get("name", "unknown"))
        points.append(MetricPoint.from_aggregate(name, aggregate))
        lines.append(
            f"| {name} | {float(aggregate.get('success_rate', 0.0)):.0%} | "
            f"{float(aggregate.get('pass_at_k', 0.0)):.0%} | "
            f"{float(aggregate.get('average_steps', 0.0)):.2f} | "
            f"{float(aggregate.get('average_tokens', 0.0)):.1f} | "
            f"{float(aggregate.get('total_cost_usd', 0.0)):.6f} | "
            f"{float(aggregate.get('cost_per_success_usd', 0.0)):.6f} |"
        )
    lines += ["", pareto.to_markdown(points, pareto.analyze(points)).rstrip(), ""]
    return "\n".join(lines) + "\n"


def _render_benchmark_report(payload: dict[str, Any]) -> str:
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict):
        msg = "benchmark report has no aggregate"
        raise ValueError(msg)
    lines = [f"# Benchmark report: {payload.get('name', 'unknown')}", ""]
    model = payload.get("model")
    if isinstance(model, dict):
        lines += [f"- Model: `{model.get('label')}`", ""]
    for key in sorted(aggregate):
        lines.append(f"- `{key}`: {aggregate[key]}")
    layers = payload.get("failure_layers")
    if isinstance(layers, dict):
        lines += ["", "## Outcome by layer", ""]
        lines += [f"- `{name}`: {value}" for name, value in sorted(layers.items())]
    categories = payload.get("failure_categories")
    if isinstance(categories, dict):
        lines += ["", "## Failure categories", ""]
        lines += [f"- `{name}`: {value}" for name, value in sorted(categories.items())]
    return "\n".join(lines) + "\n"


def _trace_replay(args: argparse.Namespace) -> int:
    trace = TraceRun.from_file(Path(args.path))
    if args.as_json:
        sys.stdout.write(f"{json.dumps(build_replay(trace).to_json(), indent=2, sort_keys=True)}\n")
    else:
        sys.stdout.write(
            replay_markdown(build_replay(trace), preview_chars=int(args.preview_chars))
        )
    return 0


def _trace_diff(args: argparse.Namespace) -> int:
    left = TraceRun.from_file(Path(args.left))
    right = TraceRun.from_file(Path(args.right))
    result = diff_traces(
        left,
        right,
        left_label=args.left_label or Path(args.left).stem,
        right_label=args.right_label or Path(args.right).stem,
    )
    if args.as_json:
        sys.stdout.write(f"{json.dumps(result.to_json(), indent=2, sort_keys=True)}\n")
    else:
        sys.stdout.write(diff_markdown(result))
    return 0


def _config(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve(strict=True)
    resolved = _resolve(args, workspace)
    payload = {
        "budget": {
            "max_steps": resolved.budget.max_steps,
            "max_tokens": resolved.budget.max_tokens,
            "max_tool_calls": resolved.budget.max_tool_calls,
            "max_duration_seconds": resolved.budget.max_duration_seconds,
        },
        "options": {
            "context_strategy": resolved.options.context_strategy,
            "allow_write": resolved.options.allow_write,
            "allow_shell": resolved.options.allow_shell,
            "allowed_commands": sorted(resolved.options.allowed_commands),
            "denied_tools": sorted(resolved.options.denied_tools),
            "sandbox_backend": resolved.options.sandbox_backend,
            "docker_image": resolved.options.docker_image,
            "tool_output_limit": resolved.options.tool_output_limit,
            "artifact_offload": resolved.options.artifact_offload,
            "tool_max_retries": resolved.options.tool_max_retries,
            "network_enabled": resolved.options.network.enabled,
            "network_rules": sorted(
                f"{rule.host}/{rule.protocol}/{sorted(rule.ports)}"
                for rule in resolved.options.network.allowed
            ),
            "input_usd_per_million": resolved.options.pricing.input_usd_per_million,
            "output_usd_per_million": resolved.options.pricing.output_usd_per_million,
            "cached_input_usd_per_million": resolved.options.pricing.cached_input_usd_per_million,
        },
        "failure_categories": [category.value for category in FailureCategory],
        "sources": resolved.sources,
    }
    sys.stdout.write(f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Mini-dsh command-line interface."""
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        raise SystemExit(_doctor())
    handlers: Mapping[str, Any] = {
        "run": _run,
        "benchmark": _benchmark,
        "ablation": _ablation,
        "report": _report,
        "config": _config,
    }
    try:
        if args.command == "config":
            exit_code = _config(args)
        elif args.command == "report":
            exit_code = _report(args)
        elif args.command == "trace":
            exit_code = _trace_replay(args) if args.trace_command == "replay" else _trace_diff(args)
        else:
            handler = handlers.get(str(args.command))
            if handler is None:
                raise AssertionError(f"unhandled command: {args.command}")
            exit_code = asyncio.run(handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"mini-dsh: {exc}\n")
        exit_code = 2
    raise SystemExit(exit_code)

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
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from minidsh import __version__
from minidsh.adapters import JsonlTraceWriter, OpenAICompatibleModel, ScriptedModel
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
from minidsh.domain import RunStatus, ToolCall
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
    run.add_argument("task", help="task given to the coding agent")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--provider", choices=("scripted", "openai-compatible"), default="scripted")
    run.add_argument("--script", type=Path, help="JSON response script for the scripted provider")
    run.add_argument(
        "--endpoint", help="Chat Completions endpoint; overrides MINIDSH_MODEL_ENDPOINT"
    )
    run.add_argument("--model", help="model name; overrides MINIDSH_MODEL")
    run.add_argument(
        "--api-key-env",
        default="MINIDSH_API_KEY",
        help="environment variable containing the provider API key",
    )
    run.add_argument("--trace", type=Path, help="JSONL trace path")

    benchmark = subparsers.add_parser("benchmark", help="run a scripted benchmark manifest")
    _add_common_options(benchmark)
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--output", type=Path, default=Path("benchmark-results/report.json"))
    benchmark.add_argument("--markdown", type=Path, help="optional Markdown report path")
    benchmark.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="allow one success-criterion executable; repeat as needed",
    )

    ablation = subparsers.add_parser("ablation", help="run a single-variable ablation suite")
    _add_common_options(ablation)
    ablation.add_argument("manifest", type=Path)
    ablation.add_argument("--output", type=Path, default=Path("benchmark-results/ablation.json"))
    ablation.add_argument(
        "--suite",
        default="standard",
        help="variant suite: standard, context, tool_output, permission",
    )
    ablation.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="allow one success-criterion executable; repeat as needed",
    )

    config = subparsers.add_parser("config", help="print the effective layered configuration")
    _add_common_options(config)
    config.add_argument("--workspace", type=Path, default=Path.cwd())
    return parser


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


def _create_model(args: argparse.Namespace) -> Model:
    provider = str(args.provider)
    if provider == "scripted":
        script = args.script
        if not isinstance(script, Path):
            msg = "--script is required for the scripted provider"
            raise ValueError(msg)
        return ScriptedModel.from_file(script)

    endpoint = args.endpoint or os.environ.get("MINIDSH_MODEL_ENDPOINT")
    model_name = args.model or os.environ.get("MINIDSH_MODEL")
    api_key = os.environ.get(str(args.api_key_env))
    if not endpoint or not model_name or not api_key:
        msg = (
            "openai-compatible requires --endpoint/MINIDSH_MODEL_ENDPOINT, "
            "--model/MINIDSH_MODEL, and the configured API-key environment variable"
        )
        raise ValueError(msg)
    return OpenAICompatibleModel(endpoint=endpoint, model=model_name, api_key=api_key)


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
    model = await asyncio.to_thread(_create_model, args)
    run_id = uuid.uuid4().hex
    trace_path = (
        Path(args.trace)
        if args.trace is not None
        else workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=resolved.options,
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
        "run_id": result.run_id,
        "status": result.status.value,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "tokens": result.token_usage.total_tokens,
        "trace": str(trace_path),
    }
    sys.stdout.write(f"{json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True)}\n")
    return 0 if result.status is RunStatus.FINISHED else 1


def _benchmark_factory(
    base: RuntimeOptions,
) -> Callable[[Path, BenchmarkTask], LocalRuntime]:
    def factory(workspace: Path, task: BenchmarkTask) -> LocalRuntime:
        if task.response_script is None:
            msg = f"benchmark task {task.task_id} requires response_script"
            raise ValueError(msg)
        model = ScriptedModel.from_file(task.response_script)
        return build_local_runtime(
            workspace=workspace,
            model=model,
            options=_task_options(base, task),
            budget=task.budget,
        )

    return factory


async def _benchmark(args: argparse.Namespace) -> int:
    manifest = await asyncio.to_thread(BenchmarkManifest.from_file, Path(args.manifest))
    resolved = await asyncio.to_thread(_resolve, args, Path.cwd())
    verification_allowlist = frozenset(str(item) for item in args.verify_command)
    if not verification_allowlist:
        msg = "benchmark requires at least one explicit --verify-command"
        raise ValueError(msg)
    output = await asyncio.to_thread(Path(args.output).resolve)
    runner = BenchmarkRunner(
        _benchmark_factory(resolved.options),
        output.parent / f"{output.stem}-artifacts",
        verification_allowlist=verification_allowlist,
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
    verification_allowlist = frozenset(str(item) for item in args.verify_command)
    if not verification_allowlist:
        msg = "ablation requires at least one explicit --verify-command"
        raise ValueError(msg)
    variants = VariantRegistry().get(str(args.suite))
    output = await asyncio.to_thread(Path(args.output).resolve)

    def factory(variant: HarnessVariant) -> BenchmarkRunner:
        base = _variant_options(resolved.options, variant)
        return BenchmarkRunner(
            _benchmark_factory(base),
            output.parent / f"{output.stem}-{variant.name}",
            verification_allowlist=verification_allowlist,
        )

    report: AblationReport = await AblationRunner(factory).run(manifest, variants)
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, report.to_json() + "\n", encoding="utf-8")
    jsonl = output.parent / f"{output.stem}.jsonl"
    await asyncio.to_thread(jsonl.write_text, report.to_jsonl(), encoding="utf-8")
    markdown = output.with_suffix(".md")
    await asyncio.to_thread(markdown.write_text, report.to_markdown(), encoding="utf-8")
    sys.stdout.write(f"{report.to_markdown()}\n")
    return (
        0 if all(item.report.aggregate()["success_rate"] == 1.0 for item in report.variants) else 1
    )


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
        },
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
        "config": _config,
    }
    handler = handlers.get(str(args.command))
    if handler is None:
        raise AssertionError(f"unhandled command: {args.command}")
    try:
        exit_code = _config(args) if args.command == "config" else asyncio.run(handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"mini-dsh: {exc}\n")
        exit_code = 2
    raise SystemExit(exit_code)

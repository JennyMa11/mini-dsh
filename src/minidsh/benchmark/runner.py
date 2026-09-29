"""Isolated, assertion-based benchmark execution."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.adapters import JsonlTraceWriter
from minidsh.benchmark.hashes import frozen_tree_digest
from minidsh.benchmark.memory import MemorySample
from minidsh.composition import LocalRuntime
from minidsh.domain import AgentBudget, Event, RunStatus
from minidsh.evaluation import TraceError, TraceRun, classify_failure, latency_from_trace
from minidsh.evaluation.inference import inference_from_trace
from minidsh.infrastructure import (
    CommandDeniedError,
    CommandPolicy,
    LocalCommandRunner,
    NetworkPolicy,
    NetworkRule,
    WorkspaceSandbox,
)

_BLOCKED_CODES = frozenset({"command_denied", "sandbox_violation"})
_FULL_SHA256_LENGTH = len("sha256:") + 64


class BenchmarkError(RuntimeError):
    """Invalid manifest or benchmark execution configuration."""


@dataclass(frozen=True, slots=True)
class TaskIsolation:
    """Pinned isolation configuration applied to one task."""

    sandbox_backend: str = "local"
    allow_write: bool = True
    allow_shell: bool = False
    allowed_commands: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    network: NetworkPolicy = field(default_factory=NetworkPolicy)

    def to_json(self) -> dict[str, object]:
        """Return a stable serialization for the report."""
        return {
            "sandbox_backend": self.sandbox_backend,
            "allow_write": self.allow_write,
            "allow_shell": self.allow_shell,
            "allowed_commands": list(self.allowed_commands),
            "denied_tools": list(self.denied_tools),
            "network": {
                "enabled": self.network.enabled,
                "rules": [
                    {
                        "host": rule.host,
                        "protocol": rule.protocol,
                        "ports": sorted(rule.ports),
                    }
                    for rule in sorted(self.network.allowed, key=lambda item: item.host)
                ],
            },
        }


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    """One pinned coding task with a fixed input, budget, and success criterion."""

    task_id: str
    goal: str
    workspace: Path
    success_argv: tuple[str, ...]
    response_script: Path | None = None
    test_argv: tuple[str, ...] | None = None
    build_argv: tuple[str, ...] | None = None
    # Files copied over the workspace *after* the agent finishes and *before* the
    # success command runs.  This is what makes a verifier independent: without it
    # the success command can only re-run tests the agent could read and edit, so it
    # measures compliance with visible tests rather than correctness against a
    # specification.  The overlay must contain verifier files only — anything a
    # solution is expected to produce would be clobbered by it.
    verification_overlay: Path | None = None
    timeout_seconds: float = 120.0
    tags: tuple[str, ...] = ()
    category: str = "general"
    repo_commit: str | None = None
    verification_commit: str | None = None
    solution_commit: str | None = None
    seed: int = 0
    model: str = "scripted"
    budget: AgentBudget = field(default_factory=AgentBudget)
    isolation: TaskIsolation = field(default_factory=TaskIsolation)


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    """A versioned reproducible collection of tasks."""

    name: str
    schema_version: str
    tasks: tuple[BenchmarkTask, ...]

    @classmethod
    def from_file(cls, path: Path) -> BenchmarkManifest:
        """Validate a JSON manifest and resolve all paths relative to it."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            parsed = _ManifestInput.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            msg = f"invalid benchmark manifest: {path}"
            raise BenchmarkError(msg) from exc
        base = path.resolve().parent
        tasks = tuple(
            BenchmarkTask(
                task_id=item.id,
                goal=item.goal,
                workspace=(base / item.workspace).resolve(),
                success_argv=tuple(item.success_argv),
                response_script=(base / item.response_script).resolve()
                if item.response_script
                else None,
                test_argv=tuple(item.test_argv) if item.test_argv else None,
                build_argv=tuple(item.build_argv) if item.build_argv else None,
                verification_overlay=(base / item.verification_overlay).resolve()
                if item.verification_overlay
                else None,
                timeout_seconds=item.timeout_seconds,
                tags=tuple(item.tags),
                category=item.category,
                repo_commit=item.repo_commit,
                verification_commit=item.verification_commit,
                solution_commit=item.solution_commit,
                seed=item.seed,
                model=item.model,
                budget=AgentBudget(
                    max_steps=item.budget.max_steps,
                    max_tokens=item.budget.max_tokens,
                    max_tool_calls=item.budget.max_tool_calls,
                    max_duration_seconds=item.budget.max_duration_seconds,
                ),
                isolation=TaskIsolation(
                    sandbox_backend=item.isolation.sandbox_backend,
                    allow_write=item.isolation.allow_write,
                    allow_shell=item.isolation.allow_shell,
                    allowed_commands=tuple(item.isolation.allowed_commands),
                    denied_tools=tuple(item.isolation.denied_tools),
                    network=NetworkPolicy(
                        frozenset(
                            NetworkRule(rule.host, rule.protocol, frozenset(rule.ports))
                            for rule in item.isolation.network_rules
                        )
                    ),
                ),
            )
            for item in parsed.tasks
        )
        return cls(parsed.name, parsed.schema_version, tasks)


@dataclass(frozen=True, slots=True)
class TaskMetrics:
    """Measured outcome and harness statistics for one task run.

    The fields are grouped by the question they answer: ``success``/``status``/
    ``failure_*`` answer *what happened*, the token and latency fields answer *what it
    cost*, and ``retries``/``permission_denied``/``unsafe_blocked`` answer *did the
    harness behave as specified*.
    """

    task_id: str
    success: bool
    status: str
    steps: int
    tokens: int
    cost_usd: float
    tool_calls: int
    latency_ms: float
    failure_type: str | None
    verifier_exit_code: int | None
    trace_path: str
    category: str = "general"
    repo_commit: str | None = None
    seed: int = 0
    model: str = "scripted"
    retries: int = 0
    retry_successes: int = 0
    permission_denied: int = 0
    unsafe_blocked: int = 0
    run_index: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    failure_category: str = "none"
    failure_layer: str = "success"
    failure_reason: str = ""
    model_ms: float = 0.0
    tool_ms: float = 0.0
    harness_ms: float = 0.0
    ttft_ms: float | None = None
    prefill_tokens_per_second: float | None = None
    decode_tokens_per_second: float | None = None
    peak_memory_gb: float | None = None
    loop_guard_triggers: int = 0
    model_format_retries: int = 0
    process_peak_gb: float | None = None
    process_memory_source: str | None = None
    system_free_before_percent: float | None = None
    system_free_after_percent: float | None = None


@dataclass(slots=True)
class _TaskStats:
    """Harness statistics derived from the event stream of one task."""

    retries: int = 0
    retry_successes: int = 0
    permission_denied: int = 0
    unsafe_blocked: int = 0
    loop_guard_triggers: int = 0
    model_format_retries: int = 0

    async def __call__(self, event: Event) -> None:
        """Count retry, permission, and unsafe-action facts."""
        if event.name == "loop_guard_triggered":
            self.loop_guard_triggers += 1
            return
        if event.name == "model_call_completed":
            retries = event.data.get("format_retries")
            if isinstance(retries, int):
                self.model_format_retries += retries
            return
        if event.name != "tool_call_completed":
            return
        data = event.data
        attempt = data.get("attempt")
        if isinstance(attempt, int) and attempt > 1:
            self.retries += 1
            if data.get("status") == "succeeded":
                self.retry_successes += 1
        if data.get("status") == "denied":
            self.permission_denied += 1
        error = data.get("error")
        if isinstance(error, dict) and error.get("code") in _BLOCKED_CODES:
            self.unsafe_blocked += 1


def _flatten_conditions(prefix: str, payload: Mapping[str, object]) -> list[str]:
    """Render nested conditions as flat ``a.b: value`` bullets.

    Nested one level because the two facts a reader needs first — which model,
    which rate card — live at different depths, and a single nested JSON blob
    makes them equally hard to find.
    """
    rows: list[str] = []
    for key in sorted(payload):
        name = f"{prefix}.{key}" if prefix else key
        value = payload[key]
        if isinstance(value, dict):
            rows.extend(_flatten_conditions(name, cast("dict[str, object]", value)))
        else:
            rows.append(f"- `{name}`: {json.dumps(value, sort_keys=True)}")
    return rows


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Task outcomes, computed aggregate metrics, and the conditions they held under."""

    name: str
    schema_version: str
    results: tuple[TaskMetrics, ...]
    # Which model and which rate card produced these numbers.  A cost figure
    # without its tariff is not a measurement, so the conditions travel inside
    # the report instead of living in a shell history.
    model: dict[str, object] | None = None

    def aggregate(self) -> dict[str, float | int]:
        """Compute comparable success, cost, and latency metrics over all runs."""
        count = len(self.results)
        if count == 0:
            return {
                "tasks": 0,
                "runs": 0,
                "success_rate": 0.0,
                "pass_at_k": 0.0,
                "average_steps": 0.0,
                "average_tokens": 0.0,
                "average_tool_calls": 0.0,
                "average_latency_ms": 0.0,
                "average_model_ms": 0.0,
                "average_tool_ms": 0.0,
                "average_harness_ms": 0.0,
                "total_cost_usd": 0.0,
                "average_cost_usd": 0.0,
                "cost_per_success_usd": 0.0,
                "cached_input_tokens": 0,
                "cached_input_share": 0.0,
                "retry_success_rate": 0.0,
                "permission_denied": 0,
                "unsafe_blocked": 0,
            }
        retries = sum(result.retries for result in self.results)
        retry_successes = sum(result.retry_successes for result in self.results)
        successes = sum(result.success for result in self.results)
        total_cost = sum(result.cost_usd for result in self.results)
        total_input = sum(result.input_tokens for result in self.results)
        cached = sum(result.cached_input_tokens for result in self.results)
        task_ids = {result.task_id for result in self.results}
        tasks_with_success = {result.task_id for result in self.results if result.success}
        return {
            "tasks": len(task_ids),
            "runs": count,
            "success_rate": successes / count,
            "pass_at_k": len(tasks_with_success) / len(task_ids) if task_ids else 0.0,
            "average_steps": sum(result.steps for result in self.results) / count,
            "average_tokens": sum(result.tokens for result in self.results) / count,
            "average_tool_calls": sum(result.tool_calls for result in self.results) / count,
            "average_latency_ms": sum(result.latency_ms for result in self.results) / count,
            "average_model_ms": sum(result.model_ms for result in self.results) / count,
            "average_tool_ms": sum(result.tool_ms for result in self.results) / count,
            "average_harness_ms": sum(result.harness_ms for result in self.results) / count,
            "total_cost_usd": total_cost,
            "average_cost_usd": total_cost / count,
            "cost_per_success_usd": total_cost / successes if successes else 0.0,
            "cached_input_tokens": cached,
            "cached_input_share": cached / total_input if total_input else 0.0,
            "retry_success_rate": (retry_successes / retries) if retries else 0.0,
            "permission_denied": sum(result.permission_denied for result in self.results),
            "unsafe_blocked": sum(result.unsafe_blocked for result in self.results),
        }

    def failure_type_counts(self) -> dict[str, int]:
        """Count tasks by terminal failure type."""
        counts: dict[str, int] = {}
        for result in self.results:
            key = result.failure_type or "none"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def failure_category_counts(self) -> dict[str, int]:
        """Count runs by taxonomy category."""
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.failure_category] = counts.get(result.failure_category, 0) + 1
        return dict(sorted(counts.items()))

    def failure_layer_counts(self) -> dict[str, int]:
        """Count runs by responsible layer: success, harness, agent, or environment."""
        counts: dict[str, int] = {"success": 0, "harness": 0, "agent": 0, "environment": 0}
        for result in self.results:
            counts[result.failure_layer] = counts.get(result.failure_layer, 0) + 1
        return counts

    def repeat_variance(self) -> dict[str, float]:
        """Report run-to-run spread for tasks that were repeated.

        A single-sample real-model result cannot be distinguished from a lucky one,
        so the spread is a first-class output rather than a footnote.
        """
        grouped: dict[str, list[TaskMetrics]] = {}
        for result in self.results:
            grouped.setdefault(result.task_id, []).append(result)
        repeated = [items for items in grouped.values() if len(items) > 1]
        if not repeated:
            return {"repeated_tasks": 0.0, "success_spread": 0.0, "token_spread": 0.0}
        success_spreads = [
            max(item.success for item in items) - min(item.success for item in items)
            for items in repeated
        ]
        token_spreads = []
        for items in repeated:
            highest = max(item.tokens for item in items)
            lowest = min(item.tokens for item in items)
            token_spreads.append((highest - lowest) / max(1, highest))
        return {
            "repeated_tasks": float(len(repeated)),
            "success_spread": sum(success_spreads) / len(success_spreads),
            "token_spread": sum(token_spreads) / len(token_spreads),
        }

    def category_counts(self) -> dict[str, int]:
        """Count tasks by declared category."""
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.category] = counts.get(result.category, 0) + 1
        return dict(sorted(counts.items()))

    def to_json(self) -> str:
        """Serialize a stable machine-readable report."""
        payload: dict[str, object] = {
            "name": self.name,
            "schema_version": self.schema_version,
            "aggregate": self.aggregate(),
            "repeat_variance": self.repeat_variance(),
            "failure_types": self.failure_type_counts(),
            "failure_categories": self.failure_category_counts(),
            "failure_layers": self.failure_layer_counts(),
            "categories": self.category_counts(),
            "results": [asdict(result) for result in self.results],
        }
        if self.model is not None:
            # Omitted rather than written as null when unknown: an absent
            # condition means "not recorded", which is not the same claim as
            # "there was no model".
            payload["model"] = self.model
        return json.dumps(payload, indent=2, sort_keys=True)

    def to_jsonl(self) -> str:
        """Serialize one task metrics record per line for append-only analysis."""
        return "".join(json.dumps(asdict(result), sort_keys=True) + "\n" for result in self.results)

    def to_markdown(self) -> str:
        """Render a human-readable Markdown summary with a per-task table."""
        aggregate = self.aggregate()
        layers = self.failure_layer_counts()
        variance = self.repeat_variance()
        lines = [
            f"# Benchmark report: {self.name}",
            "",
            f"- Schema version: `{self.schema_version}`",
            f"- Tasks: {aggregate['tasks']} ({aggregate['runs']} runs)",
            f"- Success rate: {aggregate['success_rate']:.0%}",
            f"- Pass@k (task solved at least once): {aggregate['pass_at_k']:.0%}",
            f"- Average steps: {aggregate['average_steps']:.2f}",
            f"- Average tokens: {aggregate['average_tokens']:.1f}",
            f"- Cached input tokens: {aggregate['cached_input_tokens']} "
            f"({aggregate['cached_input_share']:.0%} of input)",
            f"- Average tool calls: {aggregate['average_tool_calls']:.2f}",
            f"- Average latency: {aggregate['average_latency_ms']:.1f} ms "
            f"(model {aggregate['average_model_ms']:.1f} / "
            f"tool {aggregate['average_tool_ms']:.1f} / "
            f"harness {aggregate['average_harness_ms']:.1f})",
            f"- Total cost: ${aggregate['total_cost_usd']:.6f}",
            f"- Cost per successful run: ${aggregate['cost_per_success_usd']:.6f}",
            f"- Retry success rate: {aggregate['retry_success_rate']:.0%}",
            f"- Permission denied: {aggregate['permission_denied']}",
            f"- Unsafe actions blocked: {aggregate['unsafe_blocked']}",
            "",
        ]
        if self.model is not None:
            lines += ["## Model and rate card", ""]
            lines += _flatten_conditions("", self.model)
            lines += [""]
        lines += [
            "## Outcome by layer",
            "",
            "| Layer | Runs | Meaning |",
            "| --- | --- | --- |",
            f"| success | {layers['success']} | finished and the criterion passed |",
            f"| harness | {layers['harness']} | the runtime correctly bounded the agent |",
            f"| agent | {layers['agent']} | the model had a working harness and still failed |",
            f"| environment | {layers['environment']} | the model provider failed |",
            "",
        ]
        if variance["repeated_tasks"]:
            lines += [
                f"- Repeated tasks: {variance['repeated_tasks']:.0f}; "
                f"mean success spread {variance['success_spread']:.2f}; "
                f"mean token spread {variance['token_spread']:.0%}",
                "",
            ]
        lines += [
            "| Task | Run | Category | Success | Steps | Tokens | Tool calls |"
            " Latency (ms) | Cost (USD) | Category of failure |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {result.task_id} | {result.run_index} | {result.category} | "
            f"{'yes' if result.success else 'no'} | {result.steps} | {result.tokens} | "
            f"{result.tool_calls} | {result.latency_ms:.1f} | {result.cost_usd:.6f} | "
            f"{result.failure_category} |"
            for result in self.results
        )
        lines += ["", "## Failure categories", ""]
        lines += [f"- `{name}`: {count}" for name, count in self.failure_category_counts().items()]
        lines += ["", "## Raw failure types", ""]
        lines += [f"- `{name}`: {count}" for name, count in self.failure_type_counts().items()]
        return "\n".join(lines) + "\n"


type RuntimeFactory = Callable[[Path, BenchmarkTask], LocalRuntime]


class BenchmarkRunner:
    """Copy each fixture, run an agent, and verify success independently."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        output_directory: Path,
        *,
        verification_allowlist: frozenset[str],
        repeats: int = 1,
        model: dict[str, object] | None = None,
    ) -> None:
        if repeats < 1:
            msg = "repeats must be at least 1"
            raise BenchmarkError(msg)
        self._runtime_factory = runtime_factory
        self._output = output_directory.resolve()
        self._verification_allowlist = verification_allowlist
        self._repeats = repeats
        # The per-run label and the report-level conditions describe the same
        # model, so they are passed as one value.  Two independent parameters
        # would let a report's rows name a different model than its conditions
        # block, which is easy to introduce and hard to notice.
        label = model.get("label") if model else None
        self._model_label = str(label) if label is not None else None
        self._model = model

    async def run(self, manifest: BenchmarkManifest) -> BenchmarkReport:
        """Run tasks sequentially for deterministic local resource use.

        Repeats are interleaved per task rather than batched per sweep so that a
        transient provider incident lands inside one task's sample instead of
        correlating across the whole suite.
        """
        self._validate_manifest(manifest)
        results = [
            await self._run_task(task, run_index)
            for task in manifest.tasks
            for run_index in range(self._repeats)
        ]
        report = BenchmarkReport(
            manifest.name, manifest.schema_version, tuple(results), model=self._model
        )
        await asyncio.to_thread(self._persist_jsonl, report)
        return report

    async def run_one(self, task: BenchmarkTask, run_index: int = 0) -> TaskMetrics:
        """Execute one validated task for a durable external study scheduler."""
        self._validate_manifest(BenchmarkManifest("single", "1.0", (task,)))
        return await self._run_task(task, run_index)

    def _persist_jsonl(self, report: BenchmarkReport) -> None:
        """Write the append-only per-task results file next to the traces."""
        destination = self._output / "results.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report.to_jsonl(), encoding="utf-8")

    def _trace_path(self, task: BenchmarkTask, run_index: int) -> Path:
        suffix = "" if run_index == 0 else f".run{run_index}"
        return self._output / "traces" / f"{task.task_id}{suffix}.jsonl"

    async def _run_task(self, task: BenchmarkTask, run_index: int = 0) -> TaskMetrics:
        with tempfile.TemporaryDirectory(prefix=f"minidsh-{task.task_id}-") as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(task.workspace, workspace, symlinks=True)
            runtime = self._runtime_factory(workspace, task)
            trace_path = self._trace_path(task, run_index)
            stats = _TaskStats()
            runtime.events.subscribe("*", JsonlTraceWriter(trace_path, truncate_on_start=True))
            runtime.events.subscribe("*", stats)
            memory = MemorySample()
            stop_sampling = asyncio.Event()
            memory_task = asyncio.create_task(memory.watch(stop_sampling))
            try:
                result = await runtime.agent.run(task.goal)
            finally:
                stop_sampling.set()
                await memory_task
            verifier_exit_code: int | None = None
            failure_type: str | None = None
            verified: bool | None = None
            if result.status is RunStatus.FINISHED:
                verified = False
                overlay_ready = True
                if task.verification_overlay is not None:
                    try:
                        _apply_overlay(task.verification_overlay, workspace)
                    except OSError:
                        # The overlay could not be laid down -- an agent that left a
                        # file where the overlay expects a directory, say.  That is a
                        # failure of this task, not of the sweep: letting it propagate
                        # would discard every result already measured.
                        overlay_ready = False
                        failure_type = "verification_overlay_failed"
                if overlay_ready:
                    try:
                        runner = LocalCommandRunner(
                            WorkspaceSandbox(workspace),
                            CommandPolicy(allowed_commands=self._verification_allowlist),
                        )
                        verification = await runner.run(
                            task.success_argv, timeout_seconds=task.timeout_seconds
                        )
                        verifier_exit_code = verification.exit_code
                        verified = verification.exit_code == 0 and not verification.timed_out
                        if verification.timed_out:
                            failure_type = "verification_timeout"
                        elif verification.exit_code != 0:
                            failure_type = "verification_failed"
                    except (CommandDeniedError, OSError):
                        failure_type = "verification_denied"
            else:
                failure_type = result.status.value
            trace = await asyncio.to_thread(_read_trace, trace_path)
            diagnosis = classify_failure(
                status=result.status.value,
                verified=verified,
                trace=trace,
                verifier_exit_code=verifier_exit_code,
                failure_reason=result.failure_reason,
            )
            latency = latency_from_trace(trace) if trace is not None else result.latency
            inference = inference_from_trace(trace)
            return TaskMetrics(
                task_id=task.task_id,
                success=result.status is RunStatus.FINISHED and verified is True,
                status=result.status.value,
                steps=result.steps,
                tokens=result.token_usage.total_tokens,
                cost_usd=result.cost_usd,
                tool_calls=result.tool_calls,
                latency_ms=result.duration_ms,
                failure_type=failure_type,
                verifier_exit_code=verifier_exit_code,
                trace_path=str(trace_path),
                category=task.category,
                repo_commit=task.repo_commit,
                seed=task.seed,
                model=self._model_label or task.model,
                retries=stats.retries,
                retry_successes=stats.retry_successes,
                permission_denied=stats.permission_denied,
                unsafe_blocked=stats.unsafe_blocked,
                run_index=run_index,
                input_tokens=result.token_usage.input_tokens,
                output_tokens=result.token_usage.output_tokens,
                cached_input_tokens=result.token_usage.cached_input_tokens,
                failure_category=diagnosis.category.value,
                failure_layer=diagnosis.layer,
                failure_reason=diagnosis.reason,
                model_ms=latency.model_ms,
                tool_ms=latency.tool_ms,
                harness_ms=latency.harness_ms,
                ttft_ms=inference["ttft_ms"],
                prefill_tokens_per_second=inference["prefill_tokens_per_second"],
                decode_tokens_per_second=inference["decode_tokens_per_second"],
                peak_memory_gb=inference["peak_memory_gb"],
                loop_guard_triggers=stats.loop_guard_triggers,
                model_format_retries=stats.model_format_retries,
                process_peak_gb=(memory.peak_bytes / 1_000_000_000)
                if memory.peak_bytes is not None
                else None,
                process_memory_source="macos_phys_footprint_v2"
                if memory.peak_bytes is not None
                else None,
                system_free_before_percent=memory.before_percent,
                system_free_after_percent=memory.after_percent,
            )

    @staticmethod
    def _validate_manifest(manifest: BenchmarkManifest) -> None:
        ids = [task.task_id for task in manifest.tasks]
        if len(ids) != len(set(ids)):
            msg = "benchmark task ids must be unique"
            raise BenchmarkError(msg)
        for task in manifest.tasks:
            if not task.workspace.is_dir():
                msg = f"benchmark workspace does not exist: {task.workspace}"
                raise BenchmarkError(msg)
            if not task.success_argv:
                msg = f"benchmark task has no success command: {task.task_id}"
                raise BenchmarkError(msg)
            # A declared overlay that is missing would silently downgrade the task to
            # "re-run the visible tests", which is exactly the weak criterion the
            # overlay exists to replace.  Fail instead of running a weaker task.
            if task.verification_overlay is not None and not task.verification_overlay.is_dir():
                msg = f"benchmark verification overlay does not exist: {task.verification_overlay}"
                raise BenchmarkError(msg)
            # Full SHA-256 pins are used by the formal study.  Older pilot suites
            # retain their short content IDs and are not silently reinterpreted.
            if (
                task.repo_commit is not None
                and len(task.repo_commit) == _FULL_SHA256_LENGTH
                and frozen_tree_digest(task.workspace) != task.repo_commit
            ):
                raise BenchmarkError(f"fixture hash mismatch: {task.task_id}")
            if (
                task.verification_commit is not None
                and len(task.verification_commit) == _FULL_SHA256_LENGTH
                and (
                    task.verification_overlay is None
                    or (frozen_tree_digest(task.verification_overlay) != task.verification_commit)
                )
            ):
                raise BenchmarkError(f"verifier hash mismatch: {task.task_id}")
            if (
                task.solution_commit is not None
                and len(task.solution_commit) == _FULL_SHA256_LENGTH
            ):
                solution_path = task.workspace.parent.parent / "solutions" / task.task_id
                if frozen_tree_digest(solution_path) != task.solution_commit:
                    raise BenchmarkError(f"solution hash mismatch: {task.task_id}")


def _apply_overlay(overlay: Path, workspace: Path) -> None:
    """Copy held-out verifier files over the workspace, after the agent has finished.

    The copy happens *after* the run so the agent cannot read or edit what it will be
    judged on, and *before* the success command so the verifier sees them.  Only
    verifier files belong in an overlay: a file the agent is expected to produce
    would be silently replaced by its own overlay entry.
    """
    shutil.copytree(overlay, workspace, dirs_exist_ok=True, symlinks=True)


def _read_trace(path: Path) -> TraceRun | None:
    """Read a trace back for derived analysis, tolerating a missing file.

    Analysis is best-effort: a trace that cannot be read must not turn a measured
    task outcome into a harness crash, so the caller falls back to in-memory metrics.
    """
    if not path.is_file():
        return None
    try:
        return TraceRun.from_file(path)
    except TraceError:
        return None


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TaskBudgetInput(_StrictInput):
    max_steps: int = Field(default=50, ge=1)
    max_tokens: int = Field(default=100_000, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_duration_seconds: float = Field(default=900.0, gt=0)


class _NetworkRuleInput(_StrictInput):
    host: str = Field(min_length=1)
    protocol: str = Field(default="tcp", pattern=r"^(tcp|udp)$")
    ports: list[int] = Field(default_factory=list)


class _IsolationInput(_StrictInput):
    sandbox_backend: str = Field(default="local", pattern=r"^(local|docker)$")
    allow_write: bool = True
    allow_shell: bool = False
    allowed_commands: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    network_rules: list[_NetworkRuleInput] = Field(default_factory=list)


class _TaskInput(_StrictInput):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    goal: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    success_argv: list[str] = Field(min_length=1)
    response_script: str | None = None
    test_argv: list[str] | None = None
    build_argv: list[str] | None = None
    verification_overlay: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    tags: list[str] = Field(default_factory=list)
    category: str = Field(default="general", pattern=r"^[a-z0-9][a-z0-9_-]*$")
    repo_commit: str | None = None
    verification_commit: str | None = None
    solution_commit: str | None = None
    seed: int = 0
    model: str = "scripted"
    budget: _TaskBudgetInput = Field(default_factory=_TaskBudgetInput)
    isolation: _IsolationInput = Field(default_factory=_IsolationInput)


class _ManifestInput(_StrictInput):
    name: str = Field(min_length=1)
    schema_version: str = "1.0"
    tasks: list[_TaskInput] = Field(min_length=1)

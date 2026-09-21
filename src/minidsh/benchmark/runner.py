"""Isolated, assertion-based benchmark execution."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.adapters import JsonlTraceWriter
from minidsh.composition import LocalRuntime
from minidsh.domain import AgentBudget, Event, RunStatus
from minidsh.infrastructure import (
    CommandDeniedError,
    CommandPolicy,
    LocalCommandRunner,
    NetworkPolicy,
    NetworkRule,
    WorkspaceSandbox,
)

_BLOCKED_CODES = frozenset({"command_denied", "sandbox_violation"})


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
    timeout_seconds: float = 120.0
    tags: tuple[str, ...] = ()
    category: str = "general"
    repo_commit: str | None = None
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
                timeout_seconds=item.timeout_seconds,
                tags=tuple(item.tags),
                category=item.category,
                repo_commit=item.repo_commit,
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
    """Measured outcome and harness statistics for one task."""

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


@dataclass(slots=True)
class _TaskStats:
    """Harness statistics derived from the event stream of one task."""

    retries: int = 0
    retry_successes: int = 0
    permission_denied: int = 0
    unsafe_blocked: int = 0

    async def __call__(self, event: Event) -> None:
        """Count retry, permission, and unsafe-action facts."""
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


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Task outcomes plus computed aggregate metrics."""

    name: str
    schema_version: str
    results: tuple[TaskMetrics, ...]

    def aggregate(self) -> dict[str, float | int]:
        """Compute comparable success and execution-cost metrics."""
        count = len(self.results)
        if count == 0:
            return {
                "tasks": 0,
                "success_rate": 0.0,
                "average_steps": 0.0,
                "average_tokens": 0.0,
                "average_tool_calls": 0.0,
                "average_latency_ms": 0.0,
                "total_cost_usd": 0.0,
                "average_cost_usd": 0.0,
                "retry_success_rate": 0.0,
                "permission_denied": 0,
                "unsafe_blocked": 0,
            }
        retries = sum(result.retries for result in self.results)
        retry_successes = sum(result.retry_successes for result in self.results)
        total_cost = sum(result.cost_usd for result in self.results)
        return {
            "tasks": count,
            "success_rate": sum(result.success for result in self.results) / count,
            "average_steps": sum(result.steps for result in self.results) / count,
            "average_tokens": sum(result.tokens for result in self.results) / count,
            "average_tool_calls": sum(result.tool_calls for result in self.results) / count,
            "average_latency_ms": sum(result.latency_ms for result in self.results) / count,
            "total_cost_usd": total_cost,
            "average_cost_usd": total_cost / count,
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

    def category_counts(self) -> dict[str, int]:
        """Count tasks by declared category."""
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.category] = counts.get(result.category, 0) + 1
        return dict(sorted(counts.items()))

    def to_json(self) -> str:
        """Serialize a stable machine-readable report."""
        return json.dumps(
            {
                "name": self.name,
                "schema_version": self.schema_version,
                "aggregate": self.aggregate(),
                "failure_types": self.failure_type_counts(),
                "categories": self.category_counts(),
                "results": [asdict(result) for result in self.results],
            },
            indent=2,
            sort_keys=True,
        )

    def to_jsonl(self) -> str:
        """Serialize one task metrics record per line for append-only analysis."""
        return "".join(json.dumps(asdict(result), sort_keys=True) + "\n" for result in self.results)

    def to_markdown(self) -> str:
        """Render a human-readable Markdown summary with a per-task table."""
        aggregate = self.aggregate()
        lines = [
            f"# Benchmark report: {self.name}",
            "",
            f"- Schema version: `{self.schema_version}`",
            f"- Tasks: {aggregate['tasks']}",
            f"- Success rate: {aggregate['success_rate']:.0%}",
            f"- Average steps: {aggregate['average_steps']:.2f}",
            f"- Average tokens: {aggregate['average_tokens']:.1f}",
            f"- Average tool calls: {aggregate['average_tool_calls']:.2f}",
            f"- Average latency: {aggregate['average_latency_ms']:.1f} ms",
            f"- Total cost: ${aggregate['total_cost_usd']:.6f}",
            f"- Retry success rate: {aggregate['retry_success_rate']:.0%}",
            f"- Permission denied: {aggregate['permission_denied']}",
            f"- Unsafe actions blocked: {aggregate['unsafe_blocked']}",
            "",
            "| Task | Category | Success | Steps | Tokens | Tool calls |"
            " Latency (ms) | Cost (USD) | Failure |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {result.task_id} | {result.category} | "
            f"{'yes' if result.success else 'no'} | {result.steps} | {result.tokens} | "
            f"{result.tool_calls} | {result.latency_ms:.1f} | {result.cost_usd:.6f} | "
            f"{result.failure_type or '-'} |"
            for result in self.results
        )
        lines += ["", "## Failure types", ""]
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
    ) -> None:
        self._runtime_factory = runtime_factory
        self._output = output_directory.resolve()
        self._verification_allowlist = verification_allowlist

    async def run(self, manifest: BenchmarkManifest) -> BenchmarkReport:
        """Run tasks sequentially for deterministic local resource use."""
        self._validate_manifest(manifest)
        results = [await self._run_task(task) for task in manifest.tasks]
        report = BenchmarkReport(manifest.name, manifest.schema_version, tuple(results))
        await asyncio.to_thread(self._persist_jsonl, report)
        return report

    def _persist_jsonl(self, report: BenchmarkReport) -> None:
        """Write the append-only per-task results file next to the traces."""
        destination = self._output / "results.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report.to_jsonl(), encoding="utf-8")

    async def _run_task(self, task: BenchmarkTask) -> TaskMetrics:
        with tempfile.TemporaryDirectory(prefix=f"minidsh-{task.task_id}-") as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(task.workspace, workspace, symlinks=True)
            runtime = self._runtime_factory(workspace, task)
            trace_path = self._output / "traces" / f"{task.task_id}.jsonl"
            await asyncio.to_thread(trace_path.unlink, missing_ok=True)
            stats = _TaskStats()
            runtime.events.subscribe("*", JsonlTraceWriter(trace_path))
            runtime.events.subscribe("*", stats)
            result = await runtime.agent.run(task.goal)
            verifier_exit_code: int | None = None
            failure_type: str | None = None
            verified = False
            if result.status is RunStatus.FINISHED:
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
            return TaskMetrics(
                task.task_id,
                result.status is RunStatus.FINISHED and verified,
                result.status.value,
                result.steps,
                result.token_usage.total_tokens,
                result.cost_usd,
                result.tool_calls,
                result.duration_ms,
                failure_type,
                verifier_exit_code,
                str(trace_path),
                category=task.category,
                repo_commit=task.repo_commit,
                seed=task.seed,
                model=task.model,
                retries=stats.retries,
                retry_successes=stats.retry_successes,
                permission_denied=stats.permission_denied,
                unsafe_blocked=stats.unsafe_blocked,
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
    timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    tags: list[str] = Field(default_factory=list)
    category: str = Field(default="general", pattern=r"^[a-z0-9][a-z0-9_-]*$")
    repo_commit: str | None = None
    seed: int = 0
    model: str = "scripted"
    budget: _TaskBudgetInput = Field(default_factory=_TaskBudgetInput)
    isolation: _IsolationInput = Field(default_factory=_IsolationInput)


class _ManifestInput(_StrictInput):
    name: str = Field(min_length=1)
    schema_version: str = "1.0"
    tasks: list[_TaskInput] = Field(min_length=1)

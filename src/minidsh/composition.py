"""Explicit composition root for a local single-agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from minidsh.adapters import JsonSessionStore
from minidsh.application import AgentLoop
from minidsh.context import (
    FullHistoryContext,
    PythonSymbolRetriever,
    RepositoryRetriever,
    RetrievalContext,
    SlidingWindowContext,
    SummaryCompactionContext,
)
from minidsh.domain import AgentBudget, ModelPricing
from minidsh.infrastructure import (
    CommandPolicy,
    DockerCommandRunner,
    LocalCommandRunner,
    NetworkPolicy,
    ProcessRunner,
    WorkspaceSandbox,
)
from minidsh.ports import ContextManager, Model
from minidsh.runtime import EventBus
from minidsh.tools import (
    GitTool,
    LocalArtifactStore,
    PatchFileTool,
    PermissionPolicy,
    ReadFileTool,
    RunCommandTool,
    SearchTool,
    ShellTool,
    ToolRegistry,
    ToolRuntime,
    WriteFileTool,
)


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Safe local runtime options selected per run."""

    context_strategy: str = "full"
    allow_write: bool = False
    allow_shell: bool = False
    allowed_commands: frozenset[str] = frozenset({"git"})
    denied_tools: frozenset[str] = frozenset()
    test_command: tuple[str, ...] | None = None
    build_command: tuple[str, ...] | None = None
    sliding_window: int = 20
    recent_messages: int = 12
    reflection_enabled: bool = False
    detailed_tool_feedback: bool = True
    sandbox_backend: str = "local"
    docker_image: str = "python:3.12-slim"
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    pricing: ModelPricing = field(default_factory=ModelPricing.free)
    tool_output_limit: int = 20_000
    artifact_offload: bool = True
    tool_max_retries: int = 1


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """Objects exposed to a CLI, benchmark runner, or embedding application."""

    agent: AgentLoop
    tools: ToolRuntime
    events: EventBus
    sessions: JsonSessionStore


def build_local_runtime(
    *,
    workspace: Path,
    model: Model,
    options: RuntimeOptions,
    budget: AgentBudget | None = None,
    events: EventBus | None = None,
) -> LocalRuntime:
    """Compose local adapters without hidden globals or import-time registration."""
    sandbox = WorkspaceSandbox(workspace)
    bus = events or EventBus()
    command_policy = CommandPolicy(
        allowed_commands=options.allowed_commands,
        network=options.network,
    )
    runner: ProcessRunner
    if options.sandbox_backend == "local":
        runner = LocalCommandRunner(sandbox, command_policy)
    elif options.sandbox_backend == "docker":
        host_runner = LocalCommandRunner(
            sandbox,
            CommandPolicy(allowed_commands=frozenset({"docker"})),
        )
        runner = DockerCommandRunner(
            sandbox,
            command_policy,
            host_runner,
            image=options.docker_image,
            network=options.network,
        )
    else:
        msg = f"unknown sandbox backend: {options.sandbox_backend}"
        raise ValueError(msg)
    registry = ToolRegistry()
    registry.register(ReadFileTool(sandbox))
    registry.register(SearchTool(sandbox))
    registry.register(GitTool(runner))
    if options.allow_write:
        registry.register(PatchFileTool(sandbox))
        registry.register(WriteFileTool(sandbox))
    if options.allow_shell:
        registry.register(ShellTool(runner))
        if options.test_command is not None:
            registry.register(RunCommandTool("test", runner, options.test_command))
        if options.build_command is not None:
            registry.register(RunCommandTool("build", runner, options.build_command))

    permissions = {"fs.read", "git.read"}
    if options.allow_write:
        permissions.add("fs.write")
    if options.allow_shell:
        permissions.add("process.exec")
    tool_runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset(permissions), options.denied_tools),
        bus,
        artifacts=LocalArtifactStore(sandbox.root) if options.artifact_offload else None,
        max_output_chars=options.tool_output_limit,
        max_retries=options.tool_max_retries,
    )
    context = _build_context(options.context_strategy, sandbox.root, options)
    sessions = JsonSessionStore(sandbox.root)
    agent = AgentLoop(
        model=model,
        context_manager=context,
        tools=tool_runtime,
        events=bus,
        budget=budget,
        pricing=options.pricing,
        reflection_enabled=options.reflection_enabled,
        detailed_tool_feedback=options.detailed_tool_feedback,
        sessions=sessions,
    )
    return LocalRuntime(agent, tool_runtime, bus, sessions)


def _build_context(strategy: str, workspace: Path, options: RuntimeOptions) -> ContextManager:
    if strategy == "full":
        return FullHistoryContext()
    if strategy == "sliding":
        return SlidingWindowContext(options.sliding_window)
    if strategy == "summary":
        return SummaryCompactionContext(options.recent_messages)
    if strategy == "retrieval":
        return RetrievalContext(FullHistoryContext(), RepositoryRetriever(workspace))
    if strategy == "retrieval_summary":
        return RetrievalContext(
            SummaryCompactionContext(options.recent_messages), RepositoryRetriever(workspace)
        )
    if strategy == "symbol":
        return RetrievalContext(
            SummaryCompactionContext(options.recent_messages),
            PythonSymbolRetriever(workspace),
            name="symbol_retrieval",
        )
    msg = f"unknown context strategy: {strategy}"
    raise ValueError(msg)

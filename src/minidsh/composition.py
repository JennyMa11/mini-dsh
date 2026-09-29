"""Explicit composition root for a local single-agent runtime."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from minidsh.adapters import JsonSessionStore
from minidsh.adapters.session_journal import JsonlSessionJournal
from minidsh.application.agent_loop import AgentLoopFactory
from minidsh.context import (
    BudgetContext,
    FullHistoryContext,
    PythonSymbolRetriever,
    RepositoryRetriever,
    RetrievalContext,
    SlidingWindowContext,
    SummaryCompactionContext,
)
from minidsh.domain import AgentBudget, ModelPricing, ToolDefinition, estimate_tokens
from minidsh.infrastructure import (
    CommandPolicy,
    DockerCommandRunner,
    LocalCommandRunner,
    NetworkPolicy,
    ProcessRunner,
    WorkspaceSandbox,
)
from minidsh.ports import (
    AgentDependencies,
    AgentFactory,
    AgentRunner,
    ContextManager,
    EventStream,
    Model,
    SessionJournal,
    SessionStore,
    ToolExecutor,
)
from minidsh.runtime import EventBus, Plugin, PluginManager, PluginManifest, ServiceRegistry
from minidsh.tools import (
    FindSymbolTool,
    GitTool,
    ListFilesTool,
    LocalArtifactStore,
    PatchFileTool,
    PermissionAuthorizer,
    PermissionDecision,
    PermissionPolicy,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    SearchTool,
    ShellTool,
    Tool,
    ToolRegistry,
    ToolRuntime,
    WriteFileTool,
)
from minidsh.tools import ToolBundle as ToolBundle  # noqa: PLC0414 - public compatibility export

MAX_SUBAGENTS = 32
MAX_SUBAGENT_PARALLEL = 8


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Safe local runtime options selected per run."""

    context_strategy: str = "full"
    context_input_budget: int = 8_192
    local_first_enabled: bool = False
    error_recovery_enabled: bool = True
    progressive_tools_enabled: bool = False
    normalize_tool_results: bool = False
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
    enabled_tools: frozenset[str] | None = None
    builtin_tools_enabled: bool = True
    extra_permissions: frozenset[str] = frozenset()
    system_prompt: str = "You are a coding agent. Use tools to complete the task."
    multi_agent_enabled: bool = False
    subagent_max_agents: int = 4
    subagent_max_parallel: int = 2
    subagent_max_steps: int = 8
    subagent_max_tokens: int = 10_000
    subagent_max_tool_calls: int = 16
    subagent_max_seconds: float = 60.0

    def __post_init__(self) -> None:
        limits = (
            self.subagent_max_agents,
            self.subagent_max_parallel,
            self.subagent_max_steps,
            self.subagent_max_tokens,
            self.subagent_max_tool_calls,
            self.subagent_max_seconds,
        )
        if any(not math.isfinite(limit) or limit <= 0 for limit in limits):
            raise ValueError("all subagent limits must be positive")
        if (
            self.subagent_max_agents > MAX_SUBAGENTS
            or self.subagent_max_parallel > MAX_SUBAGENT_PARALLEL
        ):
            raise ValueError("subagent limits exceed supported bounds (32 agents, 8 parallel)")
        reserved = self.extra_permissions & {"fs.read", "fs.write", "git.read", "process.exec"}
        if reserved:
            msg = (
                f"use the dedicated capability options for reserved permissions: {sorted(reserved)}"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RuntimeComponents:
    """Trusted, explicit adapters; omitted components use safe local defaults."""

    context: ContextManager | None = None
    sessions: SessionStore | None = None
    journal: SessionJournal | None = None
    agent_factory: AgentFactory | None = None
    permissions: PermissionAuthorizer | None = None
    runner: ProcessRunner | None = None
    tools: tuple[Tool, ...] = ()
    tool_wrapper: Callable[[ToolExecutor], ToolExecutor] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Selected plugin service keys; no names are resolved implicitly."""

    model: str | None = None
    context: str | None = None
    sessions: str | None = None
    journal: str | None = None
    agent_factory: str | None = None
    permissions: str | None = None
    runner: str | None = None
    events: str | None = None
    tools: str | None = None


@dataclass(frozen=True, slots=True)
class _RestrictedPermissions:
    baseline: PermissionPolicy
    custom: PermissionAuthorizer

    def authorize(self, definition: ToolDefinition) -> PermissionDecision:
        decision = self.baseline.authorize(definition)
        return self.custom.authorize(definition) if decision.allowed else decision


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """Objects exposed to a CLI, benchmark runner, or embedding application."""

    agent: AgentRunner
    tools: ToolExecutor
    events: EventStream
    sessions: SessionStore
    journal: SessionJournal


def build_local_runtime(  # noqa: PLR0913 - explicit composition dependencies
    *,
    workspace: Path,
    model: Model,
    options: RuntimeOptions,
    budget: AgentBudget | None = None,
    events: EventStream | None = None,
    components: RuntimeComponents | None = None,
) -> LocalRuntime:
    """Compose local adapters without hidden globals or import-time registration."""
    if options.multi_agent_enabled:
        raise ValueError("use build_multi_agent_runtime to compose Multi-Agent mode")
    sandbox = WorkspaceSandbox(workspace)
    bus = events if events is not None else EventBus()
    adapters = components or RuntimeComponents()
    runner = adapters.runner
    if runner is None:
        runner = _build_runner(sandbox, options)
    registry = _build_tools(sandbox, runner, options, adapters.tools)

    permissions = {"fs.read", "git.read"}
    permissions.update(options.extra_permissions)
    if options.allow_write:
        permissions.add("fs.write")
    if options.allow_shell:
        permissions.add("process.exec")
    baseline = PermissionPolicy(frozenset(permissions), options.denied_tools)
    authorizer: PermissionAuthorizer = baseline
    if adapters.permissions is not None:
        authorizer = _RestrictedPermissions(baseline, adapters.permissions)
    tool_runtime: ToolExecutor = ToolRuntime(
        registry,
        authorizer,
        bus,
        artifacts=LocalArtifactStore(sandbox.root) if options.artifact_offload else None,
        max_output_chars=options.tool_output_limit,
        max_retries=options.tool_max_retries,
        normalize_results=options.normalize_tool_results,
    )
    if adapters.tool_wrapper is not None:
        tool_runtime = adapters.tool_wrapper(tool_runtime)
    context = adapters.context
    if context is None:
        context = _build_context(
            options.context_strategy,
            sandbox.root,
            options,
            model=model,
            tools=tool_runtime.definitions(),
        )
    sessions = (
        adapters.sessions if adapters.sessions is not None else JsonSessionStore(sandbox.root)
    )
    journal = adapters.journal if adapters.journal is not None else JsonlSessionJournal(workspace)
    factory = adapters.agent_factory if adapters.agent_factory is not None else AgentLoopFactory()
    agent = factory.create(
        AgentDependencies(
            model=model,
            context_manager=context,
            tools=tool_runtime,
            events=bus,
            budget=budget if budget is not None else AgentBudget(),
            pricing=options.pricing,
            system_prompt=options.system_prompt,
            reflection_enabled=options.reflection_enabled,
            detailed_tool_feedback=options.detailed_tool_feedback,
            sessions=sessions,
            journal=journal,
            local_first_enabled=options.local_first_enabled,
            error_recovery_enabled=options.error_recovery_enabled,
        )
    )
    return LocalRuntime(agent, tool_runtime, bus, sessions, journal)


def _build_runner(sandbox: WorkspaceSandbox, options: RuntimeOptions) -> ProcessRunner:
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
    return runner


def _build_tools(
    sandbox: WorkspaceSandbox,
    runner: ProcessRunner,
    options: RuntimeOptions,
    extra_tools: tuple[Tool, ...],
) -> ToolRegistry:
    builtin_tools: list[Tool] = [ReadFileTool(sandbox), SearchTool(sandbox), GitTool(runner)]
    if options.progressive_tools_enabled:
        builtin_tools.extend(
            (ListFilesTool(sandbox), SearchCodeTool(sandbox), FindSymbolTool(sandbox))
        )
    if options.allow_write:
        builtin_tools.extend((PatchFileTool(sandbox), WriteFileTool(sandbox)))
    if options.allow_shell:
        builtin_tools.append(ShellTool(runner))
        if options.test_command is not None:
            builtin_tools.append(RunCommandTool("test", runner, options.test_command))
        if options.build_command is not None:
            builtin_tools.append(RunCommandTool("build", runner, options.build_command))

    registry = ToolRegistry()
    if not options.builtin_tools_enabled:
        builtin_tools = []
    available = {tool.definition.name for tool in (*builtin_tools, *extra_tools)}
    if options.enabled_tools is not None:
        unknown = options.enabled_tools - available
        if unknown:
            msg = f"unknown enabled tools (or missing capability grant): {sorted(unknown)}"
            raise ValueError(msg)
    for tool in (*builtin_tools, *extra_tools):
        if options.enabled_tools is None or tool.definition.name in options.enabled_tools:
            registry.register(tool)

    return registry


@asynccontextmanager
async def plugin_runtime(  # noqa: PLR0913 - managed composition boundary
    *,
    workspace: Path,
    options: RuntimeOptions,
    plugins: Sequence[Plugin],
    selection: RuntimeServices | None = None,
    model: Model | None = None,
    budget: AgentBudget | None = None,
) -> AsyncIterator[LocalRuntime]:
    """Start explicitly supplied plugins, compose selected services, and always stop."""
    selection = selection or RuntimeServices()
    manager = PluginManager()
    if selection.agent_factory is None:
        manager.register(AgentLoopPlugin())
    for plugin in plugins:
        manager.register(plugin)
    try:
        await manager.start()
        services = manager.services
        # mypy assumes type[T] will be instantiated; these protocols are only
        # used for isinstance checks in ServiceRegistry.get.
        if selection.model is not None:
            model = services.get(selection.model, Model)  # type: ignore[type-abstract]
        if model is None:
            msg = "a model adapter or selected model service is required"
            raise ValueError(msg)
        components = RuntimeComponents(
            agent_factory=services.get(
                selection.agent_factory or "agent_loop_factory",
                AgentFactory,  # type: ignore[type-abstract]
            ),
            journal=(
                services.get(selection.journal, SessionJournal)  # type: ignore[type-abstract]
                if selection.journal is not None
                else None
            ),
            context=(
                services.get(selection.context, ContextManager)  # type: ignore[type-abstract]
                if selection.context is not None
                else None
            ),
            sessions=(
                services.get(selection.sessions, SessionStore)  # type: ignore[type-abstract]
                if selection.sessions is not None
                else None
            ),
            permissions=(
                services.get(
                    selection.permissions,
                    PermissionAuthorizer,  # type: ignore[type-abstract]
                )
                if selection.permissions is not None
                else None
            ),
            runner=(
                services.get(selection.runner, ProcessRunner)  # type: ignore[type-abstract]
                if selection.runner is not None
                else None
            ),
            tools=(
                services.get(selection.tools, ToolBundle).tools
                if selection.tools is not None
                else ()
            ),
        )
        events = (
            services.get(selection.events, EventStream)  # type: ignore[type-abstract]
            if selection.events is not None
            else None
        )
        yield build_local_runtime(
            workspace=workspace,
            model=model,
            options=options,
            budget=budget,
            events=events,
            components=components,
        )
    finally:
        await manager.stop()


class AgentLoopPlugin:
    """Mount the default loop as a managed service, like other runtime providers."""

    manifest = PluginManifest("minidsh_agent_loop", "1.0.0", provides=("agent_loop_factory",))

    async def start(self, services: ServiceRegistry) -> None:
        """Register the driver factory without constructing an implicit runtime."""
        services.register("agent_loop_factory", AgentLoopFactory())

    async def stop(self) -> None:
        """The factory owns no running tasks; callers own their run lifecycle."""


def _build_context(  # noqa: PLR0911 - explicit strategy selection remains readable
    strategy: str,
    workspace: Path,
    options: RuntimeOptions,
    *,
    model: Model | None = None,
    tools: tuple[ToolDefinition, ...] = (),
) -> ContextManager:
    if strategy == "budget":
        counter = getattr(model, "count_tokens", None)
        count = (
            (lambda messages: counter(messages, tools)) if callable(counter) else estimate_tokens
        )
        return BudgetContext(options.context_input_budget, count)
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

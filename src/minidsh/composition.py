"""Explicit composition root for a local single-agent runtime."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from minidsh.adapters import JsonSessionStore
from minidsh.adapters.conversations import JsonConversationRepository
from minidsh.adapters.file_backend import BackendFileTool, DockerFileBackend, LocalFileBackend
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.session_journal import JsonlSessionJournal
from minidsh.application.agent_loop import AgentLoopFactory
from minidsh.application.context_assembly import ContextAssembly
from minidsh.application.conversations import ConversationService
from minidsh.context import (
    BudgetContext,
    FullHistoryContext,
    PythonSymbolRetriever,
    RepositoryRetriever,
    RetrievalContext,
    SlidingWindowContext,
    SummaryCompactionContext,
)
from minidsh.context.snapshots import SnapshotRetriever
from minidsh.domain import AgentBudget, ModelPricing, RunResult, ToolDefinition, estimate_tokens
from minidsh.domain.memory import MemoryConfig
from minidsh.domain.verification import VerificationConfig
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
    ApprovalProvider,
    ContextManager,
    EventStream,
    Model,
    SessionJournal,
    SessionStore,
    StreamingModel,
    ToolExecutor,
)
from minidsh.ports.files import FileBackend
from minidsh.ports.memory import MemoryEvidenceSource, MemoryStore
from minidsh.runtime import EventBus, Plugin, PluginManager, PluginManifest, ServiceRegistry
from minidsh.runtime.approvals import DenyApproval
from minidsh.runtime.catalog import PluginSpec
from minidsh.runtime.hooks import HookRegistry
from minidsh.runtime.memory import MemoryService, ProposeMemoryTool, RetrieveMemoryTool
from minidsh.runtime.memory_maintenance import memory_maintenance_tools
from minidsh.runtime.memory_runner import MemoryAgentRunner
from minidsh.runtime.skills import DiscoverSkillsTool, LoadSkillTool, SkillService
from minidsh.runtime.verification import VerificationExtension
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
    streaming_enabled: bool = False
    approval_mode: str = "configured"
    plugin_specs: tuple[PluginSpec, ...] = ()
    service_selection: dict[str, str] = field(default_factory=dict)
    context_input_budget: int = 8_192
    local_first_enabled: bool = False
    workspace_instructions_enabled: bool = False
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
    skill_roots: tuple[str, ...] = ()
    verification: VerificationConfig | None = None
    memory: MemoryConfig | None = None

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
class RuntimeEnvironment:
    """Pair file and process capabilities from one execution world."""

    runner: ProcessRunner
    files: FileBackend
    host_storage_isolated: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeComponents:
    """Trusted, explicit adapters; omitted components use safe local defaults."""

    context: ContextManager | None = None
    approvals: ApprovalProvider | None = None
    hooks: HookRegistry | None = None
    files: FileBackend | None = None
    environment: RuntimeEnvironment | None = None
    sessions: SessionStore | None = None
    journal: SessionJournal | None = None
    agent_factory: AgentFactory | None = None
    permissions: PermissionAuthorizer | None = None
    runner: ProcessRunner | None = None
    tools: tuple[Tool, ...] = ()
    tool_wrapper: Callable[[ToolExecutor], ToolExecutor] | None = None
    memory_store: MemoryStore | None = None
    memory_evidence: MemoryEvidenceSource | None = None


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
    environment: str | None = None
    hooks: str | None = None
    memory_store: str | None = None
    memory_evidence: str | None = None


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
    conversations: ConversationService | None = None
    close: Callable[[], Awaitable[None]] | None = None
    context: ContextManager | None = None
    files: FileBackend | None = None
    initialize: Callable[[], Awaitable[None]] | None = None
    streaming_capable: bool = False
    memory_maintenance: Callable[[str], ToolExecutor] | None = None


def build_local_runtime(  # noqa: PLR0912, PLR0913, PLR0915 - explicit composition dependencies
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
    runner = adapters.environment.runner if adapters.environment else adapters.runner
    if runner is None:
        runner = _build_runner(sandbox, options)
    file_tools: tuple[Tool, ...] = (
        ReadFileTool(sandbox),
        SearchTool(sandbox),
        ListFilesTool(sandbox),
        SearchCodeTool(sandbox),
        FindSymbolTool(sandbox),
        WriteFileTool(sandbox),
        PatchFileTool(sandbox),
    )
    file_backend = adapters.environment.files if adapters.environment else adapters.files
    if file_backend is None:
        if options.sandbox_backend == "docker":
            file_runner = DockerCommandRunner(
                sandbox,
                CommandPolicy(allowed_commands=frozenset({"python3"})),
                LocalCommandRunner(sandbox, CommandPolicy(allowed_commands=frozenset({"docker"}))),
                image=options.docker_image,
            )
            file_backend = DockerFileBackend(file_runner)
        else:
            file_backend = LocalFileBackend(file_tools, workspace=sandbox.root)
    skills = SkillService(file_backend, options.skill_roots) if options.skill_roots else None
    extra_tools = (*adapters.tools, LoadSkillTool(skills)) if skills else adapters.tools
    memory_store: MemoryStore | None = None
    memory: MemoryService | None = None
    if options.memory is not None:
        configured_memory = options.memory
        memory_store = adapters.memory_store
        if memory_store is None:
            memory_store = SQLiteMemoryStore(
                Path(configured_memory.database),
                allowed_root=Path(configured_memory.storage_root),
                workspace=sandbox.root,
            )
        evidence = adapters.memory_evidence
        if evidence is None and configured_memory.evidence_root is not None:
            evidence = JsonMemoryEvidenceSource(
                Path(configured_memory.evidence_root),
                workspace=sandbox.root,
                grants=configured_memory.evidence_grants,
            )
        memory = MemoryService(
            memory_store,
            project_id=configured_memory.project_id,
            applicable_version=configured_memory.applicable_version,
            evidence=evidence,
            forbidden_content_sha256=configured_memory.forbidden_content_sha256,
            detailed_tool_feedback=options.detailed_tool_feedback,
        )
        extra_tools = (*extra_tools, ProposeMemoryTool(memory))
    if options.verification is not None:
        configured = options.verification
        if options.test_command is not None and options.test_command != configured.argv:
            raise ValueError("test_command conflicts with the fixed verification argv")
        if options.enabled_tools is not None and "test" not in options.enabled_tools:
            raise ValueError("enabled Verification requires test in enabled_tools")
        extra_tools = (
            *extra_tools,
            RunCommandTool(
                "test",
                runner,
                configured.argv,
                cwd=configured.cwd,
                timeout_seconds=configured.timeout_seconds,
            ),
        )
    if skills and options.enabled_tools is not None and "load_skill" not in options.enabled_tools:
        raise ValueError("enabled Skills require load_skill in enabled_tools")
    registry = _build_tools(sandbox, runner, options, extra_tools, file_backend)
    if memory is not None:
        _check_memory_isolation(options, adapters, file_backend, registry)
    hooks = adapters.hooks or (HookRegistry() if skills or options.verification or memory else None)
    verification = (
        VerificationExtension(options.verification, bus, hooks.is_final_finish_checker)
        if options.verification is not None and hooks is not None
        else None
    )
    release_skills: Callable[[], None] | None = None
    release_verification: Callable[[], None] | None = None
    release_memory: Callable[[], None] | None = None
    if hooks is not None:
        hooks.bind_events(bus)

    permissions = {"fs.read", "git.read"}
    permissions.update(options.extra_permissions)
    if options.allow_write:
        permissions.add("fs.write")
    if options.allow_shell:
        permissions.add("process.exec")
    if memory is not None:
        permissions.update({"memory.read", "memory.evidence.read", "memory.propose"})
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
        approvals=adapters.approvals
        or (DenyApproval() if options.approval_mode == "ask" else None),
        hooks=hooks,
    )
    if adapters.tool_wrapper is not None:
        tool_runtime = adapters.tool_wrapper(tool_runtime)
    context = adapters.context
    if isinstance(context, ContextAssembly):
        context = context.base
    context_registry = ToolRegistry()
    context_registry.register(BackendFileTool(ListFilesTool(sandbox), file_backend))
    context_registry.register(BackendFileTool(ReadFileTool(sandbox), file_backend))
    if skills is not None:
        context_registry.register(DiscoverSkillsTool(skills))
    if memory is not None:
        context_registry.register(RetrieveMemoryTool(memory))
    context_tools = ToolRuntime(
        context_registry,
        authorizer,
        bus,
        max_output_chars=options.tool_output_limit,
        max_retries=options.tool_max_retries,
        hooks=hooks,
    )
    if context is None:
        context = _build_context(
            options.context_strategy,
            sandbox.root,
            options,
            model=model,
            tools=tool_runtime.definitions(),
            context_tools=context_tools,
        )
    elif isinstance(context, RetrievalContext) and isinstance(context.retriever, SnapshotRetriever):
        context = replace(
            context,
            retriever=SnapshotRetriever(
                context_tools,
                symbols=context.name == "symbol_retrieval",
                exclude_skills=bool(skills),
            ),
        )
    elif (skills or memory) and isinstance(context, RetrievalContext):
        raise ValueError("Skills/Memory require controlled snapshot retrieval")
    counter = getattr(model, "count_tokens", None)
    context = ContextAssembly(
        context,
        skills=skills,
        tools=context_tools,
        count_tokens=(lambda messages: counter(messages, tool_runtime.definitions()))
        if callable(counter)
        else estimate_tokens,
        memory=memory,
    )
    sessions = (
        adapters.sessions if adapters.sessions is not None else JsonSessionStore(sandbox.root)
    )
    journal = adapters.journal if adapters.journal is not None else JsonlSessionJournal(workspace)
    factory = adapters.agent_factory if adapters.agent_factory is not None else AgentLoopFactory()
    if memory is not None and hooks is not None:
        release_memory = hooks.register_v2(
            "project_memory", "1.0", before_model=memory.before_model, after_tool=memory.after_tool
        )
    if skills is not None and hooks is not None:

        async def ended(result: RunResult) -> None:
            skills.release(result.run_id)

        release_skills = hooks.register_v2("project_skills", "1.0", on_run_end=ended)
    if verification is not None and hooks is not None:
        release_verification = hooks.register_v2(
            "runtime_verification",
            "1.0",
            before_finish=verification.before_finish,
            on_run_end=verification.on_run_end,
        )

    dependencies = AgentDependencies(
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
        workspace_instructions_enabled=options.workspace_instructions_enabled,
        streaming_enabled=options.streaming_enabled,
        hooks=hooks,
        request_input_budget=options.context_input_budget,
    )
    agent = factory.create(dependencies)
    if memory is not None:
        agent = MemoryAgentRunner(agent, memory)

    def conversation_factory(
        bound_journal: SessionJournal, bound_store: SessionStore
    ) -> AgentRunner:
        bound_agent = factory.create(
            replace(dependencies, journal=bound_journal, sessions=bound_store)
        )
        return MemoryAgentRunner(bound_agent, memory) if memory is not None else bound_agent

    conversations = ConversationService(
        JsonConversationRepository(workspace), sessions, conversation_factory
    )

    async def close() -> None:
        if skills is not None:
            skills.clear()
        if release_skills is not None:
            release_skills()
        if verification is not None:
            verification.clear()
        if release_verification is not None:
            release_verification()
        if release_memory is not None:
            release_memory()
        if memory_store is not None:
            await memory_store.close()

    def maintenance(operator_id: str) -> ToolExecutor:
        if memory is None:
            raise ValueError("memory_not_enabled")
        maintenance_registry = ToolRegistry()
        for tool in memory_maintenance_tools(memory, operator_id=operator_id):
            maintenance_registry.register(tool)
        policy = PermissionPolicy(frozenset({"memory.maintain", "memory.evidence.read"}))
        maintenance_authorizer: PermissionAuthorizer = policy
        if adapters.permissions is not None:
            maintenance_authorizer = _RestrictedPermissions(policy, adapters.permissions)
        return ToolRuntime(
            maintenance_registry,
            maintenance_authorizer,
            bus,
            max_output_chars=options.tool_output_limit,
        )

    return LocalRuntime(
        agent,
        tool_runtime,
        bus,
        sessions,
        journal,
        conversations,
        close=close,
        context=context,
        files=file_backend,
        streaming_capable=bool(
            getattr(model, "native_streaming", isinstance(model, StreamingModel))
        ),
        memory_maintenance=maintenance if memory is not None else None,
    )


def _check_memory_isolation(
    options: RuntimeOptions, adapters: RuntimeComponents, files: FileBackend, registry: ToolRegistry
) -> None:
    isolated = (
        adapters.environment is not None and adapters.environment.host_storage_isolated is True
    ) or (
        adapters.environment is None
        and adapters.runner is None
        and adapters.files is None
        and options.sandbox_backend == "docker"
    )
    if not isolated and (
        type(files) is not LocalFileBackend
        or any(
            set(tool.permissions) & {"process.exec", "git.read"} for tool in registry.definitions()
        )
    ):
        raise ValueError("memory_requires_isolated_execution")


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
    file_backend: FileBackend,
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
        if options.test_command is not None and options.verification is None:
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
            registered = tool
            if tool in builtin_tools and any(
                permission.startswith("fs.") for permission in tool.definition.permissions
            ):
                registered = BackendFileTool(tool, file_backend)
            registry.register(registered)

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
            hooks=services.get(selection.hooks, HookRegistry)
            if selection.hooks is not None
            else None,
            environment=(
                services.get(selection.environment, RuntimeEnvironment)
                if selection.environment is not None
                else None
            ),
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
            memory_store=services.get(selection.memory_store, MemoryStore)  # type: ignore[type-abstract]
            if selection.memory_store is not None
            else None,
            memory_evidence=services.get(selection.memory_evidence, MemoryEvidenceSource)  # type: ignore[type-abstract]
            if selection.memory_evidence is not None
            else None,
        )
        events = (
            services.get(selection.events, EventStream)  # type: ignore[type-abstract]
            if selection.events is not None
            else None
        )
        runtime = build_local_runtime(
            workspace=workspace,
            model=model,
            options=options,
            budget=budget,
            events=events,
            components=components,
        )
        try:
            yield runtime
        finally:
            if runtime.close is not None:
                await runtime.close()
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


def _build_context(  # noqa: PLR0911, PLR0913 - explicit strategy and capability selection
    strategy: str,
    workspace: Path,
    options: RuntimeOptions,
    *,
    model: Model | None = None,
    tools: tuple[ToolDefinition, ...] = (),
    context_tools: ToolExecutor | None = None,
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
        return RetrievalContext(
            FullHistoryContext(),
            SnapshotRetriever(context_tools, exclude_skills=bool(options.skill_roots))
            if context_tools
            else RepositoryRetriever(workspace),
        )
    if strategy == "retrieval_summary":
        return RetrievalContext(
            SummaryCompactionContext(options.recent_messages),
            SnapshotRetriever(context_tools, exclude_skills=bool(options.skill_roots))
            if context_tools
            else RepositoryRetriever(workspace),
        )
    if strategy == "symbol":
        return RetrievalContext(
            SummaryCompactionContext(options.recent_messages),
            SnapshotRetriever(context_tools, symbols=True, exclude_skills=bool(options.skill_roots))
            if context_tools
            else PythonSymbolRetriever(workspace),
            name="symbol_retrieval",
        )
    msg = f"unknown context strategy: {strategy}"
    raise ValueError(msg)

"""Configuration composition root with explicitly trusted, lazily started plugins."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from minidsh.adapters import JsonSessionStore
from minidsh.adapters.conversations import JsonConversationRepository
from minidsh.application.agent_loop import AgentLoopFactory
from minidsh.application.conversations import ConversationService
from minidsh.composition import (
    LocalRuntime,
    RuntimeComponents,
    RuntimeEnvironment,
    RuntimeOptions,
    RuntimeServices,
    _build_context,
    _build_runner,
    build_local_runtime,
)
from minidsh.domain import (
    AgentBudget,
    JsonValue,
    Message,
    RunResult,
    ToolCall,
    ToolDefinition,
    ToolObservation,
)
from minidsh.infrastructure import ProcessRunner, WorkspaceSandbox
from minidsh.ports import (
    AgentFactory,
    ApprovalProvider,
    ContextManager,
    EventStream,
    Model,
    SessionJournal,
    SessionStore,
)
from minidsh.ports.memory import MemoryEvidenceSource, MemoryStore
from minidsh.runtime import EventBus, PluginManager
from minidsh.runtime.catalog import PluginCatalog, PluginSpec, ValuePlugin
from minidsh.runtime.hooks import HookRegistry
from minidsh.runtime.memory import ProposeMemoryTool
from minidsh.runtime.memory_maintenance import (
    memory_maintenance_definitions,
    validate_memory_operator_id,
)
from minidsh.runtime.memory_note_tools import (
    memory_note_maintenance_definitions,
    memory_note_proposal_definition,
)
from minidsh.runtime.memory_reading import memory_reading_definitions
from minidsh.tools.runtime import PermissionAuthorizer, PermissionPolicy, ToolBundle


class _Host:
    def __init__(  # noqa: PLR0913, PLR0917 - explicit composition dependencies
        self,
        workspace: Path,
        model: Model,
        options: RuntimeOptions,
        budget: AgentBudget,
        trusted: frozenset[str],
        catalog: PluginCatalog,
        *,
        include_extension_tools: bool,
    ) -> None:
        self.workspace, self.model, self.options, self.budget = workspace, model, options, budget
        self.include_extension_tools = include_extension_tools
        self.bus = EventBus()
        self.hooks = HookRegistry()
        self.initial = build_local_runtime(
            workspace=workspace,
            model=model,
            options=replace(options, enabled_tools=None, memory=None),
            budget=budget,
            events=self.bus,
            components=RuntimeComponents(hooks=self.hooks),
        )
        self.manager = PluginManager()
        self.inner: LocalRuntime | None = None
        self.approvals: ApprovalProvider | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        selection = options.service_selection
        if "runner" in selection and "environment" not in selection:
            raise ValueError("select an environment to replace files and processes together")
        try:
            self.selection = RuntimeServices(**selection)
        except TypeError as exc:
            raise ValueError("unknown runtime service selection") from exc
        permissions = {"fs.read", "git.read", *options.extra_permissions}
        if options.allow_write:
            permissions.add("fs.write")
        if options.allow_shell:
            permissions.add("process.exec")
        if options.memory is not None:
            permissions.update(
                {"memory.read", "memory.evidence.read", "memory.propose", "memory.maintain"}
            )
            if options.memory.retrieval_mode == "progressive_v2":
                permissions.add("memory.migrate")
        defaults: dict[str, object] = {
            "model": model,
            "context": self.initial.context
            or _build_context(options.context_strategy, workspace, options),
            "sessions": self.initial.sessions,
            "journal": self.initial.journal,
            "agent_factory": AgentLoopFactory(),
            "events": self.bus,
            "runner": _build_runner(WorkspaceSandbox(workspace), options),
            "permissions": PermissionPolicy(frozenset(permissions), options.denied_tools),
            "tools": ToolBundle(()),
            "runtime_hooks": self.hooks,
        }
        assert self.initial.files is not None
        defaults["environment"] = RuntimeEnvironment(
            _build_runner(WorkspaceSandbox(workspace), options),
            self.initial.files,
            host_storage_isolated=options.sandbox_backend == "docker",
        )
        for key, value in defaults.items():
            identifier = f"minidsh_{key}"

            def factory(
                config: dict[str, JsonValue],
                *,
                plugin_id: str = identifier,
                service_key: str = key,
                service: object = value,
            ) -> ValuePlugin:
                if config:
                    raise ValueError(
                        "built-in plugin options use the runtime/profile configuration"
                    )
                return ValuePlugin(plugin_id, {service_key: service})

            catalog.register(identifier, factory)
        chosen = {spec.plugin_id for spec in options.plugin_specs}
        specs = (
            tuple(
                PluginSpec(f"minidsh_{key}") for key in defaults if f"minidsh_{key}" not in chosen
            )
            + options.plugin_specs
        )
        for plugin in catalog.instantiate(specs, trusted=trusted):
            self.manager.register(plugin)

    async def start(self) -> LocalRuntime:
        async with self._lock:
            if self._closed:
                raise ValueError("runtime has been closed")
            if self.inner is not None:
                return self.inner
            try:
                await self.manager.start()
                services, selected = self.manager.services, self.selection
                model = services.get(selected.model or "model", Model)  # type: ignore[type-abstract]
                context = services.get(selected.context or "context", ContextManager)  # type: ignore[type-abstract]
                components = RuntimeComponents(
                    context=context,
                    sessions=services.get(selected.sessions or "sessions", SessionStore),  # type: ignore[type-abstract]
                    journal=services.get(selected.journal or "journal", SessionJournal),  # type: ignore[type-abstract]
                    agent_factory=services.get(
                        selected.agent_factory or "agent_factory",
                        AgentFactory,  # type: ignore[type-abstract]
                    ),
                    permissions=services.get(
                        selected.permissions or "permissions",
                        PermissionAuthorizer,  # type: ignore[type-abstract]
                    ),
                    runner=services.get(selected.runner or "runner", ProcessRunner),  # type: ignore[type-abstract]
                    tools=services.get(selected.tools or "tools", ToolBundle).tools,
                    hooks=services.get(selected.hooks or "runtime_hooks", HookRegistry),
                    approvals=self.approvals,
                    environment=services.get(
                        selected.environment or "environment", RuntimeEnvironment
                    ),
                    memory_store=services.get(selected.memory_store, MemoryStore)  # type: ignore[type-abstract]
                    if selected.memory_store is not None
                    else None,
                    memory_evidence=services.get(selected.memory_evidence, MemoryEvidenceSource)  # type: ignore[type-abstract]
                    if selected.memory_evidence is not None
                    else None,
                )
                events = services.get(selected.events or "events", EventStream)  # type: ignore[type-abstract]
                options = self.options
                if self.include_extension_tools and options.enabled_tools is not None:
                    options = replace(
                        options,
                        enabled_tools=options.enabled_tools
                        | {tool.definition.name for tool in components.tools}
                        | ({"load_skill"} if options.skill_roots else set())
                        | ({"test"} if options.verification is not None else set())
                        | ({"propose_memory"} if options.memory is not None else set())
                        | (
                            {"search_memory", "read_memory", "propose_memory_note"}
                            if options.memory is not None
                            and options.memory.retrieval_mode == "progressive_v2"
                            else set()
                        ),
                    )
                if self.initial.close is not None:
                    await self.initial.close()
                self.inner = build_local_runtime(
                    workspace=self.workspace,
                    model=model,
                    options=options,
                    budget=self.budget,
                    events=events,
                    components=components,
                )
                if events is not self.bus:
                    self.manager.services.on_cleanup(events.subscribe("*", self.bus.publish))
                return self.inner
            except BaseException:
                await self.manager.stop()
                raise

    async def close(self) -> None:
        async with self._lock:
            runtime = self.inner or self.initial
            if runtime.close is not None:
                await runtime.close()
            await self.manager.stop()
            self.manager.services.cleanup_from(0)
            self._closed = True

    async def initialize(self) -> None:
        await self.start()


class _Agent:
    def __init__(self, host: _Host) -> None:
        self.host = host

    async def run(
        self, task: str, *, run_id: str | None = None, prior_history: tuple[Message, ...] = ()
    ) -> RunResult:
        runtime = await self.host.start()
        return await runtime.agent.run(task, run_id=run_id, prior_history=prior_history)


class _Tools:
    def __init__(self, host: _Host) -> None:
        self.host = host

    def definitions(self) -> tuple[ToolDefinition, ...]:
        if self.host.inner is not None:
            return self.host.inner.tools.definitions()
        selected = self.host.options.enabled_tools
        available = self.host.initial.tools.definitions()
        if self.host.options.memory is not None:
            available = (*available, ProposeMemoryTool.definition)
            if self.host.options.memory.retrieval_mode == "progressive_v2":
                available = (
                    *available,
                    *memory_reading_definitions()[1:],
                    memory_note_proposal_definition(),
                )
        return tuple(tool for tool in available if selected is None or tool.name in selected)

    def bind_approvals(self, provider: ApprovalProvider) -> None:
        self.host.approvals = provider
        if self.host.inner is not None:
            binder = getattr(self.host.inner.tools, "bind_approvals", None)
            if callable(binder):
                binder(provider)

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        runtime = await self.host.start()
        return await runtime.tools.execute(call, run_id, step_id)


class _MemoryMaintenance:
    def __init__(self, host: _Host, operator_id: str) -> None:
        validate_memory_operator_id(operator_id)
        self.host = host
        self.operator_id = operator_id

    def definitions(self) -> tuple[ToolDefinition, ...]:
        memory = self.host.options.memory
        return (
            *memory_maintenance_definitions(),
            *(
                memory_note_maintenance_definitions()
                if memory is not None and memory.retrieval_mode == "progressive_v2"
                else ()
            ),
        )

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        runtime = await self.host.start()
        if runtime.memory_maintenance is None:
            raise ValueError("memory_not_enabled")
        return await runtime.memory_maintenance(self.operator_id).execute(call, run_id, step_id)


class _Conversations(ConversationService):
    def __init__(self, host: _Host) -> None:
        self.host = host
        super().__init__(
            JsonConversationRepository(host.workspace),
            JsonSessionStore(host.workspace),
            lambda _journal, _sessions: _Agent(host),
        )

    async def run_turn(
        self,
        task: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        seed: tuple[Message, ...] = (),
    ) -> RunResult:
        runtime = await self.host.start()
        service = runtime.conversations
        assert service is not None
        return await service.run_turn(task, session_id=session_id, run_id=run_id, seed=seed)


def build_configured_runtime(  # noqa: PLR0913 - explicit trusted composition boundary
    *,
    workspace: Path,
    model: Model,
    options: RuntimeOptions,
    budget: AgentBudget,
    trusted_plugins: frozenset[str] = frozenset(),
    catalog: PluginCatalog | None = None,
    include_extension_tools: bool = False,
) -> LocalRuntime:
    """Discover metadata without importing untrusted plugins, then start on first operation."""
    host = _Host(
        workspace,
        model,
        options,
        budget,
        trusted_plugins,
        catalog or PluginCatalog(),
        include_extension_tools=include_extension_tools,
    )
    return replace(
        host.initial,
        agent=_Agent(host),
        tools=_Tools(host),
        conversations=_Conversations(host),
        close=host.close,
        initialize=host.initialize,
        memory_maintenance=(lambda operator_id: _MemoryMaintenance(host, operator_id))
        if options.memory is not None
        else None,
    )

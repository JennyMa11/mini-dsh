"""Contracts for explicit component injection and managed plugin composition."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.application.agent_loop import AgentLoopFactory
from minidsh.composition import (
    RuntimeComponents,
    RuntimeOptions,
    RuntimeServices,
    ToolBundle,
    build_local_runtime,
    plugin_runtime,
)
from minidsh.context import FullHistoryContext
from minidsh.domain import (
    Event,
    ModelResponse,
    PhoenixConfig,
    RunResult,
    RunStatus,
    ToolCall,
    ToolStatus,
)
from minidsh.infrastructure import ProcessResult, WorkspaceSandbox, default_opener
from minidsh.ports import AgentDependencies, AgentRunner
from minidsh.runtime import EventBus, PluginManifest, ServiceError, ServiceRegistry
from minidsh.tools import PermissionDecision, PermissionPolicy, ReadFileTool, WriteFileTool


class DenyEverything:
    def authorize(self, definition: object) -> PermissionDecision:
        del definition
        return PermissionDecision(allowed=False, reason="custom policy")


class ServicePlugin:
    def __init__(self, values: dict[str, object], order: list[str]) -> None:
        self.manifest = PluginManifest("custom", "1", provides=tuple(values))
        self.values = values
        self.order = order

    async def start(self, services: ServiceRegistry) -> None:
        self.order.append("start")
        for key, value in self.values.items():
            services.register(key, value)

    async def stop(self) -> None:
        self.order.append("stop")


@pytest.mark.asyncio
@pytest.mark.skipif(default_opener() is None, reason="no host file opener on this platform")
async def test_local_runtime_registers_open_file_with_shell(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_shell=True),
    )
    try:
        names = {tool.name for tool in runtime.tools.definitions()}
    finally:
        assert runtime.close is not None
        await runtime.close()
    assert "open_file" in names


@pytest.mark.asyncio
async def test_runtime_subscribes_phoenix_to_every_run_and_shuts_it_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[_FakePhoenix] = []

    def factory(config: PhoenixConfig, run_attributes: dict[str, object]) -> _FakePhoenix:
        subscriber = _FakePhoenix(config, run_attributes)
        captured.append(subscriber)
        return subscriber

    monkeypatch.setattr("minidsh.composition._phoenix_subscriber", factory)
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("done")]),
        options=RuntimeOptions(phoenix=PhoenixConfig(project_name="harness-a")),
    )

    result = await runtime.agent.run("task")

    assert result.answer == "done"
    assert len(captured) == 1
    assert {"run_started", "run_finished"} <= set(captured[0].events)
    assert captured[0].config.project_name == "harness-a"
    assert captured[0].run_attributes["tool_output_limit"] == 20_000
    assert captured[0].run_attributes["context_strategy"] == "full"
    assert runtime.close is not None
    await runtime.close()
    assert captured[0].shutdown_called is True


class _FakePhoenix:
    def __init__(self, config: PhoenixConfig, run_attributes: dict[str, object]) -> None:
        self.config = config
        self.run_attributes = run_attributes
        self.events: list[str] = []
        self.shutdown_called = False

    async def __call__(self, event: Event) -> None:
        self.events.append(event.name)

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_plugin_can_replace_the_loop_factory_with_injected_dependencies(
    tmp_path: Path,
) -> None:
    seen: list[AgentDependencies] = []

    class RecordingFactory:
        def create(self, dependencies: AgentDependencies) -> AgentRunner:
            seen.append(dependencies)
            return AgentLoopFactory().create(dependencies)

    order: list[str] = []
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("custom loop")]),
        options=RuntimeOptions(),
        plugins=(ServicePlugin({"loop": RecordingFactory()}, order),),
        selection=RuntimeServices(agent_factory="loop"),
    ) as runtime:
        result = await runtime.agent.run("task")
        assert result.answer == "custom loop"
        assert len(seen) == 1
        assert seen[0].tools is runtime.tools
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_injected_permission_policy_runs_in_the_standard_pipeline(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(),
        components=RuntimeComponents(permissions=DenyEverything()),
    )
    observation = await runtime.tools.execute(ToolCall("c", "read_file", {"path": "a"}), "r", 1)
    assert observation.status is ToolStatus.DENIED
    assert observation.error is not None
    assert observation.error.message == "custom policy"


@pytest.mark.asyncio
async def test_extra_tools_do_not_gain_write_permissions(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(enabled_tools=frozenset({"write_file"})),
        components=RuntimeComponents(tools=(WriteFileTool(WorkspaceSandbox(tmp_path)),)),
    )
    observation = await runtime.tools.execute(
        ToolCall("c", "write_file", {"path": "a", "content": "unsafe"}), "r", 1
    )
    assert observation.status is ToolStatus.DENIED
    assert not (tmp_path / "a").exists()


def test_explicit_empty_tool_selection_removes_all_builtins(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(enabled_tools=frozenset()),
    )
    assert runtime.tools.definitions() == ()


def test_unknown_tool_selection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown enabled tools"):
        build_local_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(enabled_tools=frozenset({"typo"})),
        )


@pytest.mark.asyncio
async def test_selected_plugin_services_drive_a_complete_run_and_cleanup(tmp_path: Path) -> None:
    order: list[str] = []
    model = ScriptedModel([ModelResponse("plugin answer")])
    extension = ServicePlugin(
        {"custom_model": model, "custom_context": FullHistoryContext()}, order
    )
    async with plugin_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(system_prompt="Follow the project rules."),
        plugins=(extension,),
        selection=RuntimeServices(model="custom_model", context="custom_context"),
    ) as runtime:
        result = await runtime.agent.run("task")
        assert result.status is RunStatus.FINISHED
        assert result.answer == "plugin answer"
        assert result.history[0].content == "Follow the project rules."
        assert order == ["start"]
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_bad_selected_service_type_rolls_back_plugins(tmp_path: Path) -> None:
    order: list[str] = []
    with pytest.raises(ServiceError, match="is not Model"):
        async with plugin_runtime(
            workspace=tmp_path,
            options=RuntimeOptions(),
            plugins=(ServicePlugin({"bad": object()}, order),),
            selection=RuntimeServices(model="bad"),
        ):
            pytest.fail("invalid service must never yield a runtime")
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_plugin_tools_share_validation_and_explicit_denial(tmp_path: Path) -> None:
    order: list[str] = []
    tool = ReadFileTool(WorkspaceSandbox(tmp_path))
    tool.definition = replace(tool.definition, name="custom_read")
    bundle = ToolBundle((tool,))
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(
            enabled_tools=frozenset({"custom_read"}), denied_tools=frozenset({"custom_read"})
        ),
        plugins=(ServicePlugin({"custom_tools": bundle}, order),),
        selection=RuntimeServices(tools="custom_tools"),
    ) as runtime:
        invalid = await runtime.tools.execute(ToolCall("a", "custom_read", {}), "r", 1)
        denied = await runtime.tools.execute(ToolCall("b", "custom_read", {"path": "a"}), "r", 1)
        assert invalid.status is ToolStatus.INVALID
        assert denied.status is ToolStatus.DENIED
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_plugin_cleanup_after_caller_failure(tmp_path: Path) -> None:
    order: list[str] = []
    with pytest.raises(RuntimeError, match="caller failed"):
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(),
            plugins=(ServicePlugin({}, order),),
        ):
            raise RuntimeError("caller failed")
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_custom_policy_cannot_override_run_capability_grants(tmp_path: Path) -> None:

    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(enabled_tools=frozenset({"write_file"})),
        components=RuntimeComponents(
            permissions=PermissionPolicy(frozenset({"fs.write"})),
            tools=(WriteFileTool(WorkspaceSandbox(tmp_path)),),
        ),
    )
    result = await runtime.tools.execute(
        ToolCall("c", "write_file", {"path": "a", "content": "unsafe"}), "r", 1
    )
    assert result.status is ToolStatus.DENIED
    assert not (tmp_path / "a").exists()


@pytest.mark.asyncio
async def test_plugin_cleanup_on_cancellation(tmp_path: Path) -> None:

    order: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(),
            plugins=(ServicePlugin({}, order),),
        ):
            raise asyncio.CancelledError
    assert order == ["start", "stop"]


@pytest.mark.asyncio
async def test_plugin_session_events_and_runner_are_injected(tmp_path: Path) -> None:

    class MemorySessions:
        def __init__(self) -> None:
            self.saved: dict[str, RunResult] = {}

        async def save(self, result: RunResult) -> None:
            self.saved[result.run_id] = result

        async def load(self, run_id: str) -> RunResult | None:
            return self.saved.get(run_id)

    class FakeRunner:
        async def run(
            self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30.0
        ) -> ProcessResult:
            del cwd, timeout_seconds
            return ProcessResult(argv, 0, "plugin output", "")

    sessions = MemorySessions()
    events = EventBus()
    order: list[str] = []
    plugin = ServicePlugin({"sessions": sessions, "events": events, "runner": FakeRunner()}, order)
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("done")]),
        options=RuntimeOptions(),
        plugins=(plugin,),
        selection=RuntimeServices(sessions="sessions", events="events", runner="runner"),
    ) as runtime:
        assert runtime.events is events
        assert runtime.sessions is sessions
        result = await runtime.agent.run("task")
        assert await sessions.load(result.run_id) == result
        observation = await runtime.tools.execute(
            ToolCall("c", "git", {"action": "status"}), result.run_id, 1
        )
        assert observation.status is ToolStatus.SUCCEEDED
        assert isinstance(observation.result, dict)
        assert observation.result["stdout"] == "plugin output"
    assert order == ["start", "stop"]


def test_custom_tools_can_replace_the_builtin_toolset_explicitly(tmp_path: Path) -> None:
    custom = ReadFileTool(WorkspaceSandbox(tmp_path))
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(builtin_tools_enabled=False),
        components=RuntimeComponents(tools=(custom,)),
    )
    assert [definition.name for definition in runtime.tools.definitions()] == ["read_file"]


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.asyncio
async def test_custom_capability_requires_an_explicit_grant(
    tmp_path: Path, *, granted: bool
) -> None:
    custom = ReadFileTool(WorkspaceSandbox(tmp_path))
    custom.definition = replace(custom.definition, permissions=("custom.read",))
    (tmp_path / "a").write_text("payload", encoding="utf-8")
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(
            builtin_tools_enabled=False,
            extra_permissions=frozenset({"custom.read"}) if granted else frozenset(),
        ),
        components=RuntimeComponents(tools=(custom,)),
    )
    result = await runtime.tools.execute(ToolCall("c", "read_file", {"path": "a"}), "r", 1)
    assert result.status is (ToolStatus.SUCCEEDED if granted else ToolStatus.DENIED)

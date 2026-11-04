"""Adversarial contracts for the interactive and configurable runtime boundaries."""

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from minidsh.adapters.file_backend import BackendFileTool
from minidsh.adapters.models import ScriptedModel
from minidsh.adapters.streaming import IncrementalRedactor
from minidsh.application.agent_loop import AgentLoop
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.configured_runtime import build_configured_runtime
from minidsh.context import FullHistoryContext, RetrievalContext
from minidsh.context.snapshots import SnapshotRetriever
from minidsh.domain import (
    AgentBudget,
    JsonValue,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.domain.harness import ApprovalChoice
from minidsh.gui.session import GuiSession
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.ports import ToolExecutor
from minidsh.runtime import EventBus
from minidsh.runtime.approvals import ApprovalBroker
from minidsh.runtime.catalog import PluginCatalog, PluginSpec, ValuePlugin
from minidsh.runtime.hooks import HookRegistry
from minidsh.runtime.plugins import PluginError, PluginManager, PluginManifest
from minidsh.runtime.services import ServiceRegistry
from minidsh.tools.builtin import PatchFileTool
from minidsh.tools.runtime import PermissionPolicy, ToolError, ToolRegistry, ToolRuntime


def test_incremental_redaction_holds_split_credentials() -> None:
    sanitizer = IncrementalRedactor("my-private-key")
    outputs = [
        sanitizer.feed(fragment)
        for fragment in ("hello " * 30, "my-private-", "key password", " = hidden ", "tail " * 40)
    ]
    outputs.append(sanitizer.feed("", final=True))
    text = "".join(outputs)
    assert "my-private-key" not in text
    assert "hidden" not in text
    assert "[REDACTED]" in text


@pytest.mark.asyncio
async def test_gui_session_switch_resets_display_counts_and_run_metadata(tmp_path: Path) -> None:
    options = RuntimeOptions()
    model = ScriptedModel([ModelResponse("answer")])
    session = GuiSession(
        workspace=tmp_path,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=model, options=options
        ),
        model_label="scripted",
        options=options,
        cost_reportable=False,
    )
    await session.action("run", {"task": "task"})
    await session.wait()
    state = session.snapshot()
    run_id = state["run_id"]
    identifier = state["session_id"]
    for _ in range(2):
        await session.action("session", {"session_id": identifier})
        state = session.snapshot()
        assert not state["messages_truncated"]
        assert len(state["messages"]) == 2
        assert state["run_id"] is None
        assert state["events"] == []
        assert state["metrics"] == {}
    await session.action("fork", {"run_id": run_id})
    assert not session.snapshot()["messages_truncated"]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local", "container_contract"])
async def test_patch_preflight_mismatch_is_failure_without_approval(
    tmp_path: Path, backend: str
) -> None:
    (tmp_path / "a.py").write_text("old\n")
    approvals = ApprovalBroker()
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True),
        components=RuntimeComponents(approvals=approvals),
    )
    tools: ToolExecutor
    if backend == "container_contract":

        class FailedPreflight:
            async def prepare(
                self, operation: str, arguments: dict[str, JsonValue]
            ) -> tuple[str, str]:
                del operation, arguments
                raise ToolError("patch_mismatch", "expected text was not found")

            async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue:
                del operation, arguments
                raise AssertionError("invalid patch must not execute")

        registry = ToolRegistry()
        registry.register(
            BackendFileTool(PatchFileTool(WorkspaceSandbox(tmp_path)), FailedPreflight())
        )
        tools = ToolRuntime(
            registry,
            PermissionPolicy(frozenset({"fs.write"})),
            EventBus(),
            approvals=approvals,
        )
    else:
        tools = runtime.tools
    observed = await tools.execute(
        ToolCall(
            "mismatch", "patch_file", {"path": "a.py", "expected": "missing", "replacement": "new"}
        ),
        "run",
        1,
    )
    assert observed.status is ToolStatus.FAILED
    assert observed.error is not None
    assert observed.error.code == "patch_mismatch"
    assert approvals.pending == ()
    assert (tmp_path / "a.py").read_text() == "old\n"


@pytest.mark.asyncio
async def test_cancelled_approval_has_no_effect_and_expires(tmp_path: Path) -> None:
    broker = ApprovalBroker()
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True),
        components=RuntimeComponents(approvals=broker),
    )
    pending = asyncio.create_task(
        runtime.tools.execute(
            ToolCall("write", "write_file", {"path": "a", "content": "bad"}), "run", 1
        )
    )
    await asyncio.sleep(0)
    request = broker.pending[0]
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not (tmp_path / "a").exists()
    with pytest.raises(ValueError, match="expired"):
        broker.respond(request.request_id, "run", ApprovalChoice.ALLOW)


@pytest.mark.asyncio
async def test_hook_mutation_does_not_change_original_request() -> None:
    hooks = HookRegistry()
    request = ModelRequest((Message(Role.USER, "task"),))

    async def rewrite(value: ModelRequest) -> ModelRequest:
        return ModelRequest((*value.messages, Message(Role.SYSTEM, "extra")), value.tools)

    remove = hooks.register("owned", before_request=rewrite)
    assert len((await hooks.request(request)).messages) == 2
    assert len(request.messages) == 1
    remove()
    assert await hooks.request(request) == request


@pytest.mark.asyncio
async def test_request_hook_overflow_stops_before_model(tmp_path: Path) -> None:
    hooks = HookRegistry()

    async def rewrite(value: ModelRequest) -> ModelRequest:
        return ModelRequest((*value.messages, Message(Role.SYSTEM, "x" * 4000)), value.tools)

    hooks.register("large", before_request=rewrite)
    model = ScriptedModel([ModelResponse("not called")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(context_input_budget=100),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status.value == "failed"
    assert model.requests == []


@pytest.mark.asyncio
async def test_context_reads_are_authorized_and_budgeted(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b")
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("done")]),
        options=RuntimeOptions(context_strategy="retrieval", denied_tools=frozenset({"read_file"})),
    )
    events = []

    async def collect(event):  # type: ignore[no-untyped-def]
        events.append(event)

    runtime.events.subscribe("*", collect)
    result = await runtime.agent.run("explain add calc")
    assert result.tool_calls == 2
    assert any(
        event.name == "permission_decided" and event.data.get("allowed") is False
        for event in events
    )
    assert not any(message.name == "repo_retrieval" for message in result.history)


@pytest.mark.asyncio
async def test_configured_plugin_selects_service_and_releases(tmp_path: Path) -> None:
    catalog = PluginCatalog()
    model = ScriptedModel([ModelResponse("plugin model")])
    catalog.register(
        "example", lambda _: ValuePlugin("example", {"example_model": model}), external=True
    )
    runtime = build_configured_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(
            plugin_specs=(PluginSpec("example"),), service_selection={"model": "example_model"}
        ),
        budget=AgentBudget(),
        trusted_plugins=frozenset({"example"}),
        catalog=catalog,
    )
    result = await runtime.agent.run("task")
    assert result.answer == "plugin model"
    assert runtime.close is not None
    await runtime.close()
    with pytest.raises(ValueError, match="closed"):
        await runtime.agent.run("again")


@pytest.mark.asyncio
async def test_undeclared_services_and_callbacks_are_rolled_back() -> None:
    cleaned = []

    class Undeclared:
        manifest = PluginManifest("bad", "1")

        async def start(self, services: ServiceRegistry) -> None:
            services.on_cleanup(lambda: cleaned.append(True))
            services.register("undeclared", object())

        async def stop(self) -> None:
            pass

    manager = PluginManager()
    manager.register(Undeclared())
    with pytest.raises(PluginError, match="undeclared"):
        await manager.start()
    assert not manager.services.keys
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_gui_approval_is_available_while_task_is_busy(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("write", "write_file", {"path": "a", "content": "ok"}),)
            ),
            ModelResponse("done"),
        ]
    )
    options = RuntimeOptions(allow_write=True, approval_mode="ask")
    session = GuiSession(
        workspace=tmp_path,
        model_label="offline",
        options=options,
        cost_reportable=False,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=model, options=options
        ),
    )
    await session.action("run", {"task": "write"})
    async with asyncio.timeout(5), aclosing(session.follow()) as updates:
        async for state in updates:
            if state["approvals"]:
                pending = state["approvals"][0]
                break
    assert session.busy
    assert not (tmp_path / "a").exists()
    await session.action(
        "approval",
        {"request_id": pending["request_id"], "run_id": pending["run_id"], "choice": "allow"},
    )
    await session.wait()
    assert (tmp_path / "a").read_text() == "ok"
    await session.close()


@pytest.mark.asyncio
async def test_model_cannot_overwrite_session_facts(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path, model=ScriptedModel([]), options=RuntimeOptions(allow_write=True)
    )
    observation = await runtime.tools.execute(
        ToolCall("corrupt", "write_file", {"path": ".minidsh/traces/run.jsonl", "content": "fake"}),
        "run",
        1,
    )
    assert observation.error is not None
    assert not (tmp_path / ".minidsh/traces/run.jsonl").exists()


@pytest.mark.asyncio
async def test_cancelled_context_preparation_keeps_attempted_tool_count(tmp_path: Path) -> None:
    class InterruptedReads:
        def definitions(self) -> tuple[ToolDefinition, ...]:
            return ()

        async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
            del run_id, step_id
            if call.name == "list_files":
                return ToolObservation(
                    call.call_id, call.name, ToolStatus.SUCCEEDED, {"files": ["calc.py"]}
                )
            raise asyncio.CancelledError

    context = RetrievalContext(FullHistoryContext(), SnapshotRetriever(InterruptedReads()))
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(),
        components=RuntimeComponents(context=context),
    )
    # Inject the prepared context directly: composition deliberately rebinds snapshot readers.

    loop = AgentLoop(
        model=ScriptedModel([]),
        context_manager=context,
        tools=runtime.tools,
        events=runtime.events,
        sessions=runtime.sessions,
    )
    with pytest.raises(asyncio.CancelledError):
        await loop.run("task", run_id="cancelled-context")
    saved = await runtime.sessions.load("cancelled-context")
    assert saved is not None
    assert saved.tool_calls == 2
    assert saved.unknown_usage_calls == 0

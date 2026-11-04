"""PR1 extension contracts; observers cannot rewrite execution facts."""

import asyncio
import json
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from pydantic import BaseModel

from minidsh.adapters import ScriptedModel
from minidsh.composition import (
    RuntimeComponents,
    RuntimeOptions,
    RuntimeServices,
    build_local_runtime,
    plugin_runtime,
)
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import (
    AgentBudget,
    Event,
    JsonValue,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.domain.extensions import (
    BudgetView,
    ControlledCaller,
    FinishAction,
    FinishDecision,
    FinishProposal,
    HookView,
)
from minidsh.runtime import EventBus, PluginManager, PluginManifest, ServiceRegistry
from minidsh.runtime.catalog import PluginCatalog, PluginSpec
from minidsh.runtime.hooks import HookRegistry
from minidsh.tools import PermissionPolicy
from minidsh.tools.runtime import ToolError


def view() -> HookView:
    return HookView("run", 1, BudgetView(15, 7000, 30, 10.0), (Message(Role.USER, "task"),))


def test_finish_feedback_is_bounded_and_required() -> None:
    with pytest.raises(ValueError, match="requires feedback"):
        FinishDecision(FinishAction.CONTINUE)
    with pytest.raises(ValueError, match="4096"):
        FinishDecision(FinishAction.CONTINUE, "x" * 4097)
    with pytest.raises(ValueError, match="allow"):
        FinishDecision(FinishAction.ALLOW, "unexpected")
    assert FinishDecision().action is FinishAction.ALLOW


@pytest.mark.asyncio
async def test_new_order_uses_registration_and_first_decision_stops_chain() -> None:
    hooks = HookRegistry()
    calls: list[str] = []

    async def allow(_proposal: FinishProposal) -> FinishDecision:
        calls.append("z")
        return FinishDecision()

    async def repair(_proposal: FinishProposal) -> FinishDecision:
        calls.append("a")
        return FinishDecision(FinishAction.CONTINUE, "repair")

    async def fail(_proposal: FinishProposal) -> FinishDecision:
        calls.append("b")
        return FinishDecision(FinishAction.FAIL, "stop")

    hooks.register_v2("z", "1", before_finish=allow)
    remove = hooks.register_v2("a", "1", before_finish=repair)
    hooks.register_v2("b", "1", before_finish=fail)
    proposal = FinishProposal(view(), "answer")
    assert (await hooks.decide(proposal)).action is FinishAction.CONTINUE
    assert calls == ["z", "a"]
    calls.clear()
    remove()
    remove()
    assert (await hooks.decide(proposal)).action is FinishAction.FAIL
    assert calls == ["z", "b"]


@pytest.mark.asyncio
async def test_new_observer_receives_copies_for_all_statuses_and_failure_is_isolated() -> None:
    hooks = HookRegistry()
    calls: list[ToolStatus] = []

    async def observer(observation: ToolObservation, _state: HookView) -> None:
        calls.append(observation.status)
        assert isinstance(observation.result, dict)
        observation.result["nested"] = "changed"
        raise ValueError("observer failure")

    hooks.register_v2("observer", "1", after_tool=observer)
    for status in ToolStatus:
        observation = ToolObservation("c", "test", status, {"nested": {"original": True}})
        await hooks.observe_all(observation, view())
        assert observation.result == {"nested": {"original": True}}
    assert calls == list(ToolStatus)


@pytest.mark.asyncio
async def test_v2_model_preserves_protected_messages_and_tool_capabilities() -> None:
    hooks = HookRegistry()

    async def corrupt(request: ModelRequest, _state: HookView) -> ModelRequest:
        return replace(request, messages=(Message(Role.USER, "replacement"),))

    hooks.register_v2("corrupt", "1", before_model=corrupt)
    request = ModelRequest((Message(Role.SYSTEM, "constraints"), Message(Role.USER, "task")), ())
    with pytest.raises(ValueError, match="protected"):
        await hooks.model(request, view())
    assert request.messages[0].content == "constraints"


@pytest.mark.asyncio
async def test_v2_tool_identity_and_deep_copy() -> None:
    hooks = HookRegistry()

    async def corrupt(call: ToolCall, _state: HookView) -> ToolCall:
        call.arguments["nested"] = False
        return replace(call, call_id="other")

    hooks.register_v2("corrupt", "1", before_tool=corrupt)
    call = ToolCall("original", "test", {"nested": {"original": True}})
    with pytest.raises(ValueError, match="identity"):
        await hooks.transform_call(call, view())
    assert call.arguments == {"nested": {"original": True}}


@pytest.mark.asyncio
async def test_legacy_sort_and_observer_error_remain_explicit_compatibility() -> None:
    hooks = HookRegistry()
    calls: list[str] = []

    async def z(request: ModelRequest) -> ModelRequest:
        calls.append("z")
        return request

    async def a(request: ModelRequest) -> ModelRequest:
        calls.append("a")
        return request

    async def failing(_observation: ToolObservation) -> None:
        raise ValueError("legacy observer")

    hooks.register("z", before_request=z)
    hooks.register("a", before_request=a, after_tool=failing)
    await hooks.request(ModelRequest((), ()))
    assert calls == ["a", "z"]
    with pytest.raises(ValueError, match="legacy observer"):
        await hooks.observed(ToolObservation("c", "test", ToolStatus.SUCCEEDED))


@pytest.mark.asyncio
async def test_finish_continue_uses_same_run_and_original_step_budget(tmp_path: Path) -> None:
    hooks = HookRegistry()
    proposals: list[int] = []
    ended: list[RunStatus] = []
    steps: list[int] = []

    async def repair(proposal: FinishProposal) -> FinishDecision:
        proposals.append(proposal.view.step_id)
        return FinishDecision(FinishAction.CONTINUE, "still needs repair")

    async def on_end(result: RunResult) -> None:
        ended.append(result.status)

    async def on_step(state: HookView) -> None:
        steps.append(state.step_id)

    hooks.register_v2("checker", "1", before_finish=repair, on_run_end=on_end, on_step_end=on_step)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("one"), ModelResponse("two")]),
        budget=AgentBudget(max_steps=2),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task", run_id="continued")
    assert result.run_id == "continued"
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.steps == 2
    assert proposals == [1, 2]
    assert steps == [1, 2]
    assert ended == [RunStatus.BUDGET_EXHAUSTED]
    assert any(m.name == "extension_feedback" for m in result.history)


@pytest.mark.asyncio
async def test_start_failure_calls_end_once_without_model_call(tmp_path: Path) -> None:
    hooks = HookRegistry()
    ended: list[RunStatus] = []

    async def fail(_state: HookView) -> None:
        raise ValueError("private plugin detail")

    async def on_end(result: RunResult) -> None:
        ended.append(result.status)
        raise ValueError("notification failed")

    hooks.register_v2("lifecycle", "1", on_run_start=fail, on_run_end=on_end)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.steps == 0
    assert ended == [RunStatus.FAILED]
    assert "private plugin detail" not in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_loop_tool_hooks_revalidate_and_observer_cannot_turn_success_into_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample").write_text("original")
    hooks = HookRegistry()
    statuses: list[ToolStatus] = []

    async def rewrite(call: ToolCall, _state: HookView) -> ToolCall:
        if call.call_id == "invalid":
            return replace(call, arguments={"path": 42})
        return call

    async def observe(observation: ToolObservation, state: HookView) -> None:
        assert state.run_id == "tool-hooks"
        assert state.remaining is not None
        statuses.append(observation.status)
        raise ValueError("observer is broken")

    hooks.register_v2("policy", "1", before_tool=rewrite, after_tool=observe)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("invalid", "read_file", {"path": "sample"}),
                        ToolCall("valid", "read_file", {"path": "sample"}),
                    )
                ),
                ModelResponse("done"),
            ]
        ),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("read sample", run_id="tool-hooks")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 2
    assert statuses == [ToolStatus.INVALID, ToolStatus.SUCCEEDED]
    assert (tmp_path / "sample").read_text() == "original"


@pytest.mark.asyncio
async def test_controlled_finish_read_is_accounted_paired_and_expires(tmp_path: Path) -> None:
    (tmp_path / "sample").write_text("evidence")
    hooks = HookRegistry()
    held: list[ControlledCaller] = []
    proposals: list[int] = []

    async def checker(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        held.append(proposal.caller)
        proposals.append(proposal.view.step_id)
        if len(proposals) == 1:
            before = proposal.caller.snapshot()
            observation = await proposal.caller.invoke("read_file", {"path": "sample"})
            assert observation.status is ToolStatus.SUCCEEDED
            after = proposal.caller.snapshot()
            assert before.remaining is not None
            assert after.remaining is not None
            assert after.remaining.tool_calls == before.remaining.tool_calls - 1
            assert observation.call_id.startswith("extension-")
            return FinishDecision(FinishAction.CONTINUE, "use the observed evidence")
        return FinishDecision()

    hooks.register_v2("fake", "1", before_finish=checker)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("candidate"), ModelResponse("final")]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 1
    assert result.steps == 2
    pairs = [(i, m) for i, m in enumerate(result.history) if m.role is Role.TOOL]
    assert len(pairs) == 1
    index, observation_message = pairs[0]
    assert result.history[index - 1].tool_calls[0].call_id == observation_message.tool_call_id
    for caller in held:
        with pytest.raises(RuntimeError, match="inactive"):
            await caller.invoke("read_file", {"path": "sample"})


@pytest.mark.asyncio
async def test_controlled_calls_cannot_reset_or_exceed_tool_budget(tmp_path: Path) -> None:
    hooks = HookRegistry()

    async def checker(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        await proposal.caller.invoke("read_file", {"path": "missing"})
        await proposal.caller.invoke("read_file", {"path": "other"})
        return FinishDecision()

    hooks.register_v2("budget", "1", before_finish=checker)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("done")]),
        budget=AgentBudget(max_tool_calls=1),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.tool_calls == 1
    assert len([m for m in result.history if m.role is Role.TOOL]) == 1


@pytest.mark.asyncio
async def test_controlled_capability_rejects_child_tasks_and_tool_recursion(tmp_path: Path) -> None:
    hooks = HookRegistry()
    held: list[ControlledCaller] = []
    blocked: list[str] = []

    async def before_tool(call: ToolCall, _state: HookView) -> ToolCall:
        with pytest.raises(RuntimeError, match="recursive"):
            await held[-1].invoke("read_file", {"path": "nested"})
        blocked.append("recursive")
        return call

    async def checker(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        held.append(proposal.caller)
        with pytest.raises(RuntimeError, match="owning task"):
            await asyncio.create_task(proposal.caller.invoke("read_file", {"path": "background"}))
        blocked.append("background")
        await proposal.caller.invoke("read_file", {"path": "missing"})
        return FinishDecision()

    hooks.register_v2("guard", "1", before_finish=checker, before_tool=before_tool)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("done")]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 1
    assert blocked == ["background", "recursive"]


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_hook_service_selection_and_cleanup_in_both_composition_paths(
    tmp_path: Path, *, configured: bool
) -> None:
    hooks = HookRegistry()
    seen: list[str] = []

    async def inject(request: ModelRequest, _state: HookView) -> ModelRequest:
        seen.append("inject")
        return replace(
            request,
            messages=(*request.messages, Message(Role.USER, "derived note", name="extension_note")),
        )

    class HookPlugin:
        manifest = PluginManifest("third_party_hooks", "1", provides=("custom_hooks",))

        async def start(self, services: ServiceRegistry) -> None:
            services.register("custom_hooks", hooks)
            services.on_cleanup(hooks.register_v2("injection", "1", before_model=inject))

        async def stop(self) -> None:
            seen.append("stop")

    model = ScriptedModel([ModelResponse("done")])
    if configured:
        catalog = PluginCatalog()
        catalog.register("third_party_hooks", lambda _: HookPlugin())
        runtime = build_configured_runtime(
            workspace=tmp_path,
            model=model,
            budget=AgentBudget(),
            options=RuntimeOptions(
                plugin_specs=(PluginSpec("third_party_hooks"),),
                service_selection={"hooks": "custom_hooks"},
            ),
            catalog=catalog,
        )
        try:
            result = await runtime.agent.run("task")
        finally:
            assert runtime.close is not None
            await runtime.close()
    else:
        async with plugin_runtime(
            workspace=tmp_path,
            model=model,
            options=RuntimeOptions(),
            plugins=(HookPlugin(),),
            selection=RuntimeServices(hooks="custom_hooks"),
        ) as runtime:
            result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert model.requests[0].messages[-1].content == "derived note"
    assert all(m.name != "extension_note" for m in result.history)
    assert seen == ["inject", "stop"]
    assert hooks.order_manifest() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy", ["full", "sliding", "summary", "retrieval", "retrieval_summary", "symbol", "budget"]
)
async def test_no_hooks_and_empty_registry_preserve_requests_actions_and_trace(
    tmp_path: Path, strategy: str
) -> None:
    (tmp_path / "sample").write_text("content")
    requests: list[list[ModelRequest]] = []
    results: list[RunResult] = []
    traces: list[list[tuple[str, int, JsonValue]]] = []

    def stable(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key not in {"duration_ms", "latency"}
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    for hooks in [None, HookRegistry()]:
        events: list[tuple[str, int, JsonValue]] = []
        model = ScriptedModel(
            [
                ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "sample"}),)),
                ModelResponse("done"),
            ],
            measure_input_tokens=True,
        )
        runtime = build_local_runtime(
            workspace=tmp_path,
            options=RuntimeOptions(context_strategy=strategy),
            model=model,
            components=RuntimeComponents(hooks=hooks),
        )

        async def collect(
            event: Event, destination: list[tuple[str, int, JsonValue]] = events
        ) -> None:
            # The plan excludes newly added audit events from semantic Trace equality.
            if event.name not in {"finish_decided", "extension_hook_completed"}:
                destination.append((event.name, event.step_id, stable(event.data)))

        runtime.events.subscribe("*", collect)
        results.append(await runtime.agent.run("read sample"))
        requests.append(model.requests)
        traces.append(events)
    assert results[0].status is RunStatus.FINISHED
    assert results[1].status is RunStatus.FINISHED
    assert requests[0] == requests[1]
    assert results[0].history == results[1].history
    assert results[0].token_usage == results[1].token_usage
    assert results[0].tool_calls == results[1].tool_calls
    assert traces[0] == traces[1]


@pytest.mark.asyncio
async def test_extension_failed_edit_keeps_local_first_finish_protection(tmp_path: Path) -> None:
    (tmp_path / "sample").write_text("original")
    hooks = HookRegistry()
    checks: list[int] = []

    async def checker(proposal: FinishProposal) -> FinishDecision:
        checks.append(proposal.view.step_id)
        assert proposal.caller is not None
        observation = await proposal.caller.invoke(
            "patch_file", {"path": "sample", "expected": "absent", "replacement": "changed"}
        )
        assert observation.status is ToolStatus.FAILED
        return FinishDecision()

    hooks.register_v2("failed_edit", "1", before_finish=checker)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(local_first_enabled=True, allow_write=True),
        model=ScriptedModel([ModelResponse("done") for _ in range(3)]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("repair sample")
    assert result.status is RunStatus.FAILED
    assert "unresolved_edit" in (result.failure_reason or "")
    assert checks == [1]
    assert result.tool_calls == 1
    assert (tmp_path / "sample").read_text() == "original"


@pytest.mark.asyncio
async def test_extension_calls_keep_shared_repetition_guard(tmp_path: Path) -> None:
    hooks = HookRegistry()

    async def checker(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        for _ in range(7):
            await proposal.caller.invoke("read_file", {"path": "missing"})
        return FinishDecision()

    hooks.register_v2("repeated", "1", before_finish=checker)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(local_first_enabled=True),
        model=ScriptedModel([ModelResponse("done")]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert "loop_stuck" in (result.failure_reason or "")
    assert result.tool_calls == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "model", "tool", "observed", "step", "finish", "end"])
@pytest.mark.parametrize("fault", ["exception", "timeout", "cancel"])
async def test_lifecycle_faults_close_once_without_repeating_side_effects(  # noqa: PLR0915 - explicit fault matrix
    tmp_path: Path, phase: str, fault: str
) -> None:
    hooks = HookRegistry()
    entered = asyncio.Event()
    events: list[Event] = []
    endings: list[RunStatus] = []
    closed_steps: list[int] = []

    async def trigger(where: str) -> None:
        if phase != where:
            return
        entered.set()
        if fault == "exception":
            raise ValueError("private_canary_do_not_log")
        await asyncio.Event().wait()

    async def start(_state: HookView) -> None:
        await trigger("start")

    async def model(request: ModelRequest, _state: HookView) -> ModelRequest:
        await trigger("model")
        return request

    async def tool(call: ToolCall, _state: HookView) -> ToolCall:
        await trigger("tool")
        return call

    async def observed(_observation: ToolObservation, _state: HookView) -> None:
        await trigger("observed")

    async def step(state: HookView) -> None:
        closed_steps.append(state.step_id)
        await trigger("step")

    async def finish(_proposal: FinishProposal) -> FinishDecision:
        await trigger("finish")
        return FinishDecision()

    async def end(result: RunResult) -> None:
        endings.append(result.status)
        await trigger("end")

    async def collect(event: Event) -> None:
        events.append(event)

    hooks.register_v2(
        "fault_injection",
        "1",
        on_run_start=start,
        before_model=model,
        before_tool=tool,
        after_tool=observed,
        on_step_end=step,
        before_finish=finish,
        on_run_end=end,
    )
    scripted = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("write", "write_file", {"path": "sentinel", "content": "one"}),
                )
            ),
            ModelResponse("done"),
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(allow_write=True),
        model=scripted,
        budget=AgentBudget(max_duration_seconds=0.2 if fault == "timeout" else 5),
        components=RuntimeComponents(hooks=hooks),
    )
    runtime.events.subscribe("*", collect)
    pending = asyncio.create_task(runtime.agent.run("task", run_id="fault-run"))
    if fault == "cancel":
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
    if fault == "cancel" and phase != "end":
        with pytest.raises(asyncio.CancelledError):
            await pending
        loaded = await runtime.sessions.load("fault-run")
        assert loaded is not None
        result = loaded
        expected = RunStatus.CANCELLED
    else:
        result = await pending
        if fault == "timeout" and phase != "end":
            expected = RunStatus.BUDGET_EXHAUSTED
        elif fault == "exception" and phase in {"start", "model", "finish"}:
            expected = RunStatus.FAILED
        else:
            expected = RunStatus.FINISHED
    assert entered.is_set()
    assert result.status is expected
    assert endings == [expected]
    assert closed_steps == list(range(1, result.steps + 1))
    assert len([event for event in events if event.name == "run_finished"]) == 1
    started_tools = [event for event in events if event.name == "tool_call_started"]
    assert len(started_tools) <= 1
    if phase in {"start", "model", "tool"}:
        assert not (tmp_path / "sentinel").exists()
    else:
        assert (tmp_path / "sentinel").read_text() == "one"
    assert "private_canary_do_not_log" not in json.dumps([event.data for event in events])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "tool"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_invalid_transform_is_audited_as_failure(phase: str, *, legacy: bool) -> None:
    bus = EventBus()
    events: list[Event] = []
    hooks = HookRegistry(bus)

    async def collect(event: Event) -> None:
        events.append(event)

    async def model(request: ModelRequest, _state: HookView) -> ModelRequest:
        return replace(request, messages=())

    async def tool(call: ToolCall, _state: HookView) -> ToolCall:
        return replace(call, call_id="changed")

    bus.subscribe("*", collect)

    async def legacy_model(request: ModelRequest) -> ModelRequest:
        return replace(request, tools=(ToolDefinition("extra", "extra", {"type": "object"}),))

    async def legacy_tool(call: ToolCall) -> ToolCall:
        return replace(call, call_id="changed")

    if legacy:
        hooks.register("contract", before_request=legacy_model, before_tool=legacy_tool)
    else:
        hooks.register_v2("contract", "1", before_model=model, before_tool=tool)
    with hooks.tool_scope(view()):
        request = ModelRequest((Message(Role.SYSTEM, "protected"),), ())
        call = ToolCall("original", "read_file", {"path": "sample"})
        if legacy:
            operation = hooks.request(request) if phase == "model" else hooks.call(call)
        else:
            operation = (
                hooks.model(request, view())
                if phase == "model"
                else hooks.transform_call(call, view())
            )
        with pytest.raises(ValueError, match=r"protected|identity|capability"):
            await operation
    assert len(events) == 1
    assert events[0].data["outcome"] == "failed"
    assert events[0].data["extension_id"] == "contract"
    assert events[0].schema_version == "1.0"


@pytest.mark.parametrize(
    ("owner", "version"),
    [("x" * 32768, "1"), ("normal", "x" * 32768)],
    ids=["long_id", "long_version"],
)
def test_v2_identity_is_bounded_before_audit_registration(owner: str, version: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        HookRegistry().register_v2(owner, version)


@pytest.mark.asyncio
async def test_cancellation_during_legacy_finish_does_not_repeat_callback(tmp_path: Path) -> None:
    entered = asyncio.Event()
    hooks = HookRegistry()
    calls: list[RunStatus] = []
    endings: list[RunStatus] = []

    async def legacy(result: RunResult) -> None:
        calls.append(result.status)
        entered.set()
        await asyncio.Event().wait()

    async def modern(result: RunResult) -> None:
        endings.append(result.status)

    hooks.register("legacy", before_finish=legacy)
    hooks.register_v2("modern", "1", on_run_end=modern)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("done")]),
        components=RuntimeComponents(hooks=hooks),
    )
    pending = asyncio.create_task(runtime.agent.run("task", run_id="legacy-cancel"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    result = await runtime.sessions.load("legacy-cancel")
    assert result is not None
    assert result.status is RunStatus.CANCELLED
    assert calls == [RunStatus.FINISHED]
    assert endings == [RunStatus.CANCELLED]


@pytest.mark.asyncio
async def test_legacy_and_modern_end_share_one_cleanup_deadline(tmp_path: Path) -> None:
    hooks = HookRegistry()
    endings: list[RunStatus] = []

    async def legacy(_result: RunResult) -> None:
        await asyncio.sleep(0.65)

    async def modern(result: RunResult) -> None:
        endings.append(result.status)
        await asyncio.Event().wait()

    hooks.register("legacy", before_finish=legacy)
    hooks.register_v2("modern", "1", on_run_end=modern)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("done")]),
        components=RuntimeComponents(hooks=hooks),
    )
    started = time.monotonic()
    result = await asyncio.wait_for(runtime.agent.run("task"), 3)
    assert time.monotonic() - started < 1.4
    assert result.status is RunStatus.FINISHED
    assert endings == [RunStatus.FINISHED]


@pytest.mark.asyncio
async def test_failed_plugin_start_removes_registered_v2_hooks_once() -> None:
    hooks = HookRegistry()
    calls: list[str] = []

    async def observer(_state: HookView) -> None:
        calls.append("stale")

    class BrokenPlugin:
        manifest = PluginManifest("broken_hooks", "1")

        async def start(self, services: ServiceRegistry) -> None:
            services.on_cleanup(hooks.register_v2("temporary", "1", on_run_start=observer))
            raise ValueError("startup injection")

        async def stop(self) -> None:
            calls.append("stop")

    manager = PluginManager()
    manager.register(BrokenPlugin())
    with pytest.raises(ValueError, match="startup injection"):
        await manager.start()
    await manager.stop()
    await hooks.started(view())
    assert calls == ["stop"]
    assert hooks.order_manifest() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("rewrite", ["invalid", "escape", "permission", "legal"])
async def test_tool_rewrite_rechecks_schema_permissions_and_real_paths(
    tmp_path: Path, rewrite: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample").write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    hooks = HookRegistry()
    observations: list[ToolObservation] = []

    async def transform(call: ToolCall, _state: HookView) -> ToolCall:
        path: JsonValue = (
            "../outside" if rewrite == "escape" else (42 if rewrite == "invalid" else "sample")
        )
        return replace(
            call,
            name="write_file",
            arguments={"path": path, "content": "changed", "overwrite": True},
        )

    async def observe(observation: ToolObservation, _state: HookView) -> None:
        observations.append(observation)

    hooks.register_v2("rewrite", "1", before_tool=transform, after_tool=observe)
    runtime = build_local_runtime(
        workspace=workspace,
        options=RuntimeOptions(allow_write=True),
        model=ScriptedModel(
            [
                ModelResponse(tool_calls=(ToolCall("kept", "read_file", {"path": "sample"}),)),
                ModelResponse("done"),
            ]
        ),
        components=RuntimeComponents(
            hooks=hooks,
            permissions=PermissionPolicy(frozenset({"fs.read", "git.read"}))
            if rewrite == "permission"
            else None,
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert len(observations) == 1
    observation = observations[0]
    assert observation.call_id == "kept"
    assert observation.tool_name == "write_file"
    assert outside.read_text() == "preserve"
    if rewrite == "legal":
        assert observation.status is ToolStatus.SUCCEEDED
        assert (workspace / "sample").read_text() == "changed"
    else:
        assert observation.status is not ToolStatus.SUCCEEDED
        assert (workspace / "sample").read_text() == "original"
        assert observation.error is not None
        assert (
            observation.error.code
            == {
                "invalid": "invalid_arguments",
                "escape": "sandbox_violation",
                "permission": "permission_denied",
            }[rewrite]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_legacy_tool_callbacks_cannot_invoke_an_unaccounted_nested_tool(
    tmp_path: Path, phase: str
) -> None:
    hooks = HookRegistry()
    nested = False

    async def before(call: ToolCall) -> ToolCall:
        nonlocal nested
        if phase == "before" and not nested:
            nested = True
            await runtime.tools.execute(
                ToolCall("nested", "write_file", {"path": "nested", "content": "blocked"}),
                "legacy-recursive",
                1,
            )
        return call

    async def after(_observation: ToolObservation) -> None:
        if phase == "after":
            await runtime.tools.execute(
                ToolCall("nested", "write_file", {"path": "nested", "content": "blocked"}),
                "legacy-recursive",
                1,
            )

    hooks.register("legacy", before_tool=before, after_tool=after)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(allow_write=True),
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("original", "write_file", {"path": "original", "content": "one"}),
                    )
                ),
                ModelResponse("done"),
            ]
        ),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task", run_id="legacy-recursive")
    assert result.tool_calls == 1
    assert not (tmp_path / "nested").exists()
    assert (tmp_path / "original").exists() == (phase == "after")


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_kind", ["exact", "over", "unknown"])
async def test_token_or_unknown_usage_stops_before_completion_checks(
    tmp_path: Path, usage_kind: str
) -> None:
    hooks = HookRegistry()
    checked: list[str] = []

    async def checker(_proposal: FinishProposal) -> FinishDecision:
        checked.append("unexpected")
        return FinishDecision()

    hooks.register_v2("budget", "1", before_finish=checker)
    usage = (
        TokenUsage(0, 0)
        if usage_kind == "unknown"
        else TokenUsage(8 if usage_kind == "exact" else 9, 2)
    )
    model = ScriptedModel([ModelResponse("done", usage=usage, usage_known=usage_kind != "unknown")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=model,
        budget=AgentBudget(max_tokens=10),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.token_usage == usage
    assert result.unknown_usage_calls == (1 if usage_kind == "unknown" else 0)
    assert result.tool_calls == 0
    assert len(model.requests) == 1
    assert checked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_real_tool_failure_observed_once_without_retrying_write(
    tmp_path: Path, failure: str
) -> None:
    hooks = HookRegistry()
    observations: list[ToolObservation] = []
    attempts: list[str] = []

    class EmptyInput(BaseModel):
        pass

    class Probe:
        input_model = EmptyInput
        definition = ToolDefinition(
            "probe",
            "side effect fault probe",
            {"type": "object"},
            timeout_seconds=0.01,
            side_effects=True,
            permissions=("probe.write",),
        )

        async def execute(self, _arguments: BaseModel) -> JsonValue:
            attempts.append("write")
            (tmp_path / "effect").write_text(str(len(attempts)))
            if failure == "error":
                raise ToolError("injected", "transient side effect failure", retryable=True)
            await asyncio.Event().wait()
            return None

    async def observer(observation: ToolObservation, _state: HookView) -> None:
        observations.append(observation)
        raise ValueError("observer failure must not rewrite real failure")

    hooks.register_v2("observer", "1", after_tool=observer)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(
            builtin_tools_enabled=False, extra_permissions=frozenset({"probe.write"})
        ),
        model=ScriptedModel(
            [ModelResponse(tool_calls=(ToolCall("p", "probe", {}),)), ModelResponse("done")]
        ),
        components=RuntimeComponents(hooks=hooks, tools=(Probe(),)),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 1
    assert attempts == ["write"]
    assert (tmp_path / "effect").read_text() == "1"
    assert len(observations) == 1
    assert observations[0].status is (
        ToolStatus.FAILED if failure == "error" else ToolStatus.TIMED_OUT
    )
    assert observations[0].error is not None
    assert observations[0].error.code == ("injected" if failure == "error" else "tool_timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [FinishAction.CONTINUE, FinishAction.FAIL])
async def test_finish_chain_short_circuits_and_restarts_at_next_proposal(
    tmp_path: Path, action: FinishAction
) -> None:
    hooks = HookRegistry()
    seen: list[str] = []

    async def first(_proposal: FinishProposal) -> FinishDecision:
        seen.append("z")
        return FinishDecision()

    async def second(proposal: FinishProposal) -> FinishDecision:
        seen.append("a")
        return (
            FinishDecision(action, "fake_rejection")
            if proposal.view.step_id == 1
            else FinishDecision()
        )

    async def third(_proposal: FinishProposal) -> FinishDecision:
        seen.append("b")
        return FinishDecision()

    hooks.register_v2("z", "1", before_finish=first)
    hooks.register_v2("a", "1", before_finish=second)
    hooks.register_v2("b", "1", before_finish=third)
    model = ScriptedModel(
        [ModelResponse("one", usage=TokenUsage(3, 2)), ModelResponse("two", usage=TokenUsage(4, 1))]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=model,
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task", run_id="finish-chain")
    assert result.run_id == "finish-chain"
    if action is FinishAction.FAIL:
        assert result.status is RunStatus.FAILED
        assert result.failure_reason == "fake_rejection"
        assert result.steps == 1
        assert seen == ["z", "a"]
        assert result.token_usage.total_tokens == 5
    else:
        assert result.status is RunStatus.FINISHED
        assert result.steps == 2
        assert seen == ["z", "a", "z", "a", "b"]
        assert result.token_usage.total_tokens == 10


@pytest.mark.asyncio
async def test_mixed_order_manifest_and_unload_are_repeatable() -> None:
    hooks = HookRegistry()
    calls: list[str] = []

    async def legacy(label: str, request: ModelRequest) -> ModelRequest:
        calls.append(label)
        return request

    async def modern(label: str, request: ModelRequest, _state: HookView) -> ModelRequest:
        calls.append(label)
        return request

    hooks.register("z", priority=-1, before_request=partial(legacy, "legacy_z"))
    hooks.register("b", before_request=partial(legacy, "legacy_b"))
    hooks.register("a", before_request=partial(legacy, "legacy_a"))
    hooks.register_v2("z", "2", before_model=partial(modern, "modern_z"))
    remove = hooks.register_v2("a", "2", before_model=partial(modern, "modern_a"))
    hooks.register_v2("b", "2", before_model=partial(modern, "modern_b"))
    assert [item[:2] for item in hooks.order_manifest()] == [
        ("z", "legacy_v1"),
        ("a", "legacy_v1"),
        ("b", "legacy_v1"),
        ("z", "2"),
        ("a", "2"),
        ("b", "2"),
    ]
    for _ in range(3):
        calls.clear()
        request = await hooks.request(ModelRequest((), ()))
        await hooks.model(request, view())
        assert calls == ["legacy_z", "legacy_a", "legacy_b", "modern_z", "modern_a", "modern_b"]
    remove()
    remove()
    calls.clear()
    await hooks.model(ModelRequest((), ()), view())
    assert calls == ["modern_z", "modern_b"]


@pytest.mark.asyncio
async def test_legacy_callbacks_have_versioned_run_bound_audit(tmp_path: Path) -> None:
    hooks = HookRegistry()
    events: list[Event] = []

    async def request(value: ModelRequest) -> ModelRequest:
        return value

    async def call(value: ToolCall) -> ToolCall:
        return value

    async def observed(_value: ToolObservation) -> None:
        pass

    async def finish(_value: RunResult) -> None:
        pass

    async def collect(event: Event) -> None:
        if event.name == "extension_hook_completed":
            events.append(event)

    hooks.register(
        "legacy",
        before_request=request,
        before_tool=call,
        after_tool=observed,
        before_finish=finish,
    )
    (tmp_path / "sample").write_text("data")
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel(
            [
                ModelResponse(tool_calls=(ToolCall("c", "read_file", {"path": "sample"}),)),
                ModelResponse("done"),
            ]
        ),
        components=RuntimeComponents(hooks=hooks),
    )
    runtime.events.subscribe("*", collect)
    result = await runtime.agent.run("task", run_id="legacy-audit")
    assert result.status is RunStatus.FINISHED
    assert [e.data["phase"] for e in events] == [
        "before_request",
        "before_tool",
        "after_tool",
        "before_request",
        "legacy_before_finish",
    ]
    assert all(
        e.run_id == "legacy-audit"
        and e.data["extension_id"] == "legacy"
        and e.data["extension_version"] == "legacy_v1"
        and e.data["outcome"] == "completed"
        for e in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_end_notifications_cannot_start_tools(tmp_path: Path, *, legacy: bool) -> None:
    hooks = HookRegistry()

    async def finish(result: RunResult) -> None:
        await runtime.tools.execute(
            ToolCall("late", "write_file", {"path": "late", "content": "blocked"}),
            result.run_id,
            result.steps,
        )

    if legacy:
        hooks.register("legacy", before_finish=finish)
    else:
        hooks.register_v2("modern", "1", on_run_end=finish)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(allow_write=True),
        model=ScriptedModel([ModelResponse("done")]),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.tool_calls == 0
    assert not (tmp_path / "late").exists()


@pytest.mark.asyncio
async def test_legacy_policy_exception_text_stays_out_of_loop_result_and_trace(
    tmp_path: Path,
) -> None:
    hooks = HookRegistry()
    events: list[Event] = []

    async def request(_value: ModelRequest) -> ModelRequest:
        raise ValueError("private_legacy_canary_do_not_log")

    async def collect(event: Event) -> None:
        events.append(event)

    hooks.register("legacy", before_request=request)
    model = ScriptedModel([ModelResponse("must not be called")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=model,
        components=RuntimeComponents(hooks=hooks),
    )
    runtime.events.subscribe("*", collect)
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert not model.requests
    assert "private_legacy_canary_do_not_log" not in (result.failure_reason or "")
    assert "private_legacy_canary_do_not_log" not in json.dumps([event.data for event in events])


@pytest.mark.asyncio
async def test_legacy_request_cannot_remove_system_or_current_task(tmp_path: Path) -> None:
    hooks = HookRegistry()

    async def request(value: ModelRequest) -> ModelRequest:
        return replace(value, messages=())

    hooks.register("legacy", before_request=request)
    model = ScriptedModel([ModelResponse("must not be called")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=model,
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("protected task")
    assert result.status is RunStatus.FAILED
    assert model.requests == []
    assert result.history[0].role is Role.SYSTEM
    assert result.history[-1].content == "protected task"


@pytest.mark.asyncio
async def test_final_step_observer_timeout_takes_budget_precedence(tmp_path: Path) -> None:
    hooks = HookRegistry()
    endings: list[RunStatus] = []

    async def step(_state: HookView) -> None:
        await asyncio.Event().wait()

    async def end(result: RunResult) -> None:
        endings.append(result.status)

    hooks.register_v2("observer", "1", on_step_end=step, on_run_end=end)
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(),
        model=ScriptedModel([ModelResponse("candidate")]),
        budget=AgentBudget(max_duration_seconds=0.05),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "duration budget exhausted"
    assert endings == [RunStatus.BUDGET_EXHAUSTED]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_rewritten_tool_updates_local_first_using_effective_call(
    tmp_path: Path, *, failure: bool
) -> None:
    (tmp_path / "original").write_text("original")
    hooks = HookRegistry()

    async def transform(call: ToolCall, _state: HookView) -> ToolCall:
        if failure:
            return replace(
                call,
                name="patch_file",
                arguments={"path": "original", "expected": "absent", "replacement": "changed"},
            )
        return replace(call, name="write_file", arguments={"path": "actual", "content": "changed"})

    hooks.register_v2("rewrite", "1", before_tool=transform)
    responses = [
        ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "original"}),))
    ] + [ModelResponse("candidate") for _ in range(3)]
    runtime = build_local_runtime(
        workspace=tmp_path,
        options=RuntimeOptions(allow_write=True, local_first_enabled=True),
        model=ScriptedModel(responses),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    if failure:
        assert result.status is RunStatus.FAILED
        assert "unresolved_edit" in (result.failure_reason or "")
    else:
        assert result.status is RunStatus.FINISHED
        states = [json.loads(m.content) for m in result.history if m.name == "plan_state"]
        assert states[-1]["modified_files"] == ["actual"]
        assert (tmp_path / "actual").read_text() == "changed"
    assert (tmp_path / "original").read_text() == "original"

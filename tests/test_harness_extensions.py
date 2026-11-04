"""Contracts for cancellable streaming, guarded approvals and runtime hooks."""

import asyncio
from pathlib import Path

import httpx
import pytest

from minidsh.adapters.models import CredentialRedactingModel, OpenAICompatibleModel, ScriptedModel
from minidsh.application.agent_loop import AgentLoop
from minidsh.benchmark.mini_dsh import CohortBudget, _GateModel
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.context import FullHistoryContext
from minidsh.domain import Message, ModelRequest, ModelResponse, Role, ToolCall
from minidsh.domain.harness import ApprovalChoice, StreamEvent
from minidsh.runtime import EventBus
from minidsh.runtime.approvals import ApprovalBroker
from minidsh.runtime.hooks import HookRegistry


@pytest.mark.asyncio
async def test_stream_assembles_calls_before_execution() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"Hello ","tool_calls":'
        '[{"index":0,"id":"call","function":{"name":"read_file",'
        '"arguments":"{\\"path\\":"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"world","tool_calls":'
        '[{"index":0,"function":{"arguments":"\\"a.py\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )
    model = OpenAICompatibleModel(
        endpoint="https://model.invalid/v1/chat/completions",
        model="fixed",
        max_attempts=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    )
    events = [event async for event in model.stream(ModelRequest(()))]
    assert "".join(event.text for event in events) == "Hello world"
    assert events[-1].response is not None
    assert events[-1].response.tool_calls == (ToolCall("call", "read_file", {"path": "a.py"}),)
    assert events[-1].response.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_interrupted_stream_never_runs_incomplete_tool(tmp_path: Path) -> None:
    class Interrupted:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            raise AssertionError("stream should be selected")

        async def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
            del request
            yield StreamEvent(text="partial")
            raise asyncio.CancelledError

    runtime = build_local_runtime(
        workspace=tmp_path,
        model=Interrupted(),
        options=RuntimeOptions(streaming_enabled=True),
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime.agent.run("task", run_id="cancelled")
    saved = await runtime.sessions.load("cancelled")
    assert saved is not None
    assert saved.unknown_usage_calls == 1
    assert all(message.content != "partial" for message in saved.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["bare", "credentials", "budget"])
async def test_cancel_during_stream_event_closes_provider_immediately(
    tmp_path: Path, wrapper: str
) -> None:
    closed = asyncio.Event()

    class Streaming:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            raise AssertionError("stream should be selected")

        async def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
            del request
            try:
                yield StreamEvent(text="partial " * 40)
                await asyncio.Event().wait()
            finally:
                closed.set()

    source = Streaming()
    model = (
        CredentialRedactingModel(source, "test-secret")
        if wrapper == "credentials"
        else _GateModel(source, CohortBudget())
        if wrapper == "budget"
        else source
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(streaming_enabled=True),
    )

    async def cancel(event):  # type: ignore[no-untyped-def]
        del event
        raise asyncio.CancelledError

    runtime.events.subscribe("model_text_received", cancel)
    with pytest.raises(asyncio.CancelledError):
        await runtime.agent.run("task")
    assert closed.is_set()


@pytest.mark.asyncio
async def test_approval_remembers_only_run_and_target(tmp_path: Path) -> None:
    broker = ApprovalBroker()
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True),
        components=RuntimeComponents(approvals=broker),
    )
    call = ToolCall("one", "write_file", {"path": "a.txt", "content": "first"})
    pending = asyncio.create_task(runtime.tools.execute(call, "run", 1))
    await asyncio.sleep(0)
    request = broker.pending[0]
    assert not (tmp_path / "a.txt").exists()
    broker.respond(request.request_id, "run", ApprovalChoice.REMEMBER)
    assert (await pending).error is None
    changed = ToolCall(
        "two", "write_file", {"path": "a.txt", "content": "second", "overwrite": True}
    )
    assert (await runtime.tools.execute(changed, "run", 2)).error is None
    other_run = asyncio.create_task(runtime.tools.execute(changed, "next", 1))
    await asyncio.sleep(0)
    assert len(broker.pending) == 1
    broker.respond(broker.pending[0].request_id, "next", ApprovalChoice.DENY)
    assert (await other_run).status.value == "denied"


@pytest.mark.asyncio
async def test_hard_denial_does_not_request_approval(tmp_path: Path) -> None:
    broker = ApprovalBroker()
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True),
        components=RuntimeComponents(approvals=broker),
    )
    outcome = await runtime.tools.execute(
        ToolCall("escape", "write_file", {"path": "../outside", "content": "bad"}),
        "run",
        1,
    )
    assert outcome.error is not None
    assert broker.pending == ()


@pytest.mark.asyncio
async def test_tool_hook_is_revalidated(tmp_path: Path) -> None:
    hooks = HookRegistry()

    async def change(call: ToolCall) -> ToolCall:
        return ToolCall(call.call_id, call.name, {"path": "../outside", "content": "bad"})

    hooks.register("rewrite", before_tool=change)
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True),
        components=RuntimeComponents(hooks=hooks),
    )
    observation = await runtime.tools.execute(
        ToolCall("write", "write_file", {"path": "a", "content": "ok"}),
        "run",
        1,
    )
    assert observation.error is not None
    assert not (tmp_path / "a").exists()


@pytest.mark.asyncio
async def test_request_hook_cannot_add_tools() -> None:
    hooks = HookRegistry()

    async def rewrite(request: ModelRequest) -> ModelRequest:
        return ModelRequest((*request.messages, Message(Role.SYSTEM, "extra")), request.tools)

    hooks.register("request", before_request=rewrite)
    model = ScriptedModel([ModelResponse("done")])
    bus = EventBus()
    loop = AgentLoop(
        model=model,
        context_manager=FullHistoryContext(),
        tools=_NoTools(),
        events=bus,
        hooks=hooks,
    )
    await loop.run("task")
    assert model.requests[0].messages[-1].content == "extra"


class _NoTools:
    def definitions(self):  # type: ignore[no-untyped-def]
        return ()

    async def execute(self, call: ToolCall, run_id: str, step_id: int):  # type: ignore[no-untyped-def]
        del call, run_id, step_id
        raise AssertionError("no tool should execute")

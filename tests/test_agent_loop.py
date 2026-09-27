"""Contract tests for the core agent loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from minidsh.adapters import ModelAdapterError
from minidsh.application import AgentLoop
from minidsh.domain import (
    AgentBudget,
    ContextView,
    Event,
    Message,
    ModelCallError,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    Role,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.evaluation import TraceRun, build_replay
from minidsh.evaluation.tracefile import TraceRecord


@dataclass
class FakeEvents:
    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class FullContext:
    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        del task
        return ContextView(history, "test", "1", 1)


@dataclass
class ScriptedModel:
    responses: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass
class FakeTools:
    calls: list[ToolCall] = field(default_factory=list)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ToolDefinition("read_file", "read", {"type": "object"}),)

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        del run_id, step_id
        self.calls.append(call)
        return ToolObservation(call.call_id, call.name, ToolStatus.SUCCEEDED, "contents")


@dataclass
class FailingModel:
    """Spends real time in the provider, then fails the way a provider does."""

    delay_seconds: float
    message: str = "provider unreachable"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        await asyncio.sleep(self.delay_seconds)
        raise RuntimeError(self.message)


@pytest.mark.asyncio
async def test_follow_up_keeps_history_and_resets_run_budgets() -> None:
    model = ScriptedModel([ModelResponse(content="first"), ModelResponse(content="second")])
    loop = AgentLoop(
        model=model,
        context_manager=FullContext(),
        tools=FakeTools(),
        events=FakeEvents(),
        budget=AgentBudget(max_steps=1),
    )
    first = await loop.run("one")
    second = await loop.run("two", prior_history=first.history)
    assert first.run_id != second.run_id
    assert second.steps == 1
    assert [m.content for m in model.requests[1].messages[1:]] == ["one", "first", "two"]
    assert sum(m.role is Role.SYSTEM for m in second.history) == 1


@pytest.mark.asyncio
async def test_follow_up_closes_unexecuted_tool_calls_without_mutating_trace_history() -> None:
    calls = (ToolCall("a", "read_file", {}), ToolCall("b", "read_file", {}))
    model = ScriptedModel([ModelResponse(tool_calls=calls), ModelResponse(content="recovered")])
    loop = AgentLoop(
        model=model,
        context_manager=FullContext(),
        tools=FakeTools(),
        events=FakeEvents(),
        budget=AgentBudget(max_tool_calls=1),
    )
    first = await loop.run("one")
    assert first.status is RunStatus.BUDGET_EXHAUSTED
    await loop.run("continue", prior_history=first.history)
    messages = model.requests[1].messages
    observations = [m for m in messages if m.role is Role.TOOL]
    assert [m.tool_call_id for m in observations] == ["a", "b"]
    assert json.loads(observations[-1].content)["error"]["code"] == "interrupted"
    assert len([m for m in first.history if m.role is Role.TOOL]) == 1


@pytest.mark.asyncio
async def test_loop_executes_tool_and_continues_to_finish() -> None:
    expected_steps = 2
    expected_tokens = 38
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(call,), usage=TokenUsage(10, 5)),
            ModelResponse(content="done", usage=TokenUsage(20, 3)),
        ]
    )
    tools = FakeTools()
    events = FakeEvents()
    loop = AgentLoop(model=model, context_manager=FullContext(), tools=tools, events=events)

    result = await loop.run("inspect the repository", run_id="run-1")

    assert result.status is RunStatus.FINISHED
    assert result.answer == "done"
    assert result.steps == expected_steps
    assert result.tool_calls == 1
    assert result.token_usage.total_tokens == expected_tokens
    assert tools.calls == [call]
    assert [event.name for event in events.events if event.name != "session_message_committed"] == [
        "run_started",
        "context_built",
        "model_call_started",
        "model_call_completed",
        "context_built",
        "model_call_started",
        "model_call_completed",
        "run_finished",
    ]
    assert model.requests[1].messages[-1].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_loop_stops_at_step_budget() -> None:
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    model = ScriptedModel([ModelResponse(tool_calls=(call,))])
    loop = AgentLoop(
        model=model,
        context_manager=FullContext(),
        tools=FakeTools(),
        events=FakeEvents(),
        budget=AgentBudget(max_steps=1),
    )

    result = await loop.run("never finish")

    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "step budget exhausted"


@dataclass
class CodedFailureModel:
    """Fails the way the OpenAI-compatible adapter does on a malformed response."""

    code: str
    usage: TokenUsage | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelAdapterError(
            "model returned invalid tool arguments for shell", code=self.code, usage=self.usage
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, TokenUsage(), TokenUsage(100, 20, 0.0, 60)])
async def test_failed_call_preserves_known_usage_and_trace(usage: TokenUsage | None) -> None:
    class FailingProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            raise ModelCallError("invalid action", code="invalid_tool_arguments", usage=usage)

    events = FakeEvents()
    tools = FakeTools()
    pricing = ModelPricing(2.0, 4.0, 0.5)
    loop = AgentLoop(
        model=FailingProvider(),
        context_manager=FullContext(),
        tools=tools,
        events=events,
        pricing=pricing,
        budget=AgentBudget(max_tokens=1),
    )
    result = await loop.run("fail after consuming tokens")
    expected = usage or TokenUsage()
    assert result.status is RunStatus.FAILED
    assert result.token_usage.total_tokens == expected.total_tokens
    assert result.token_usage.cached_input_tokens == expected.cached_input_tokens
    assert result.token_usage.cost_usd == pytest.approx(pricing.cost_usd(expected))
    assert tools.calls == []
    failures = [event for event in events.events if event.name == "model_call_failed"]
    assert len(failures) == 1
    assert failures[0].data["usage_known"] is (usage is not None)
    assert failures[0].data["input_tokens"] == (usage.input_tokens if usage else None)
    assert failures[0].data["duration_ms"] is not None
    assert not any(event.name == "model_call_completed" for event in events.events)
    trace = TraceRun.from_records(
        tuple(
            TraceRecord(event.name, event.run_id, event.step_id, event.data)
            for event in events.events
        )
    )
    assert build_replay(trace).tokens == (
        expected.input_tokens,
        expected.output_tokens,
        expected.cached_input_tokens,
    )
    assert trace.cost_usd() == pytest.approx(result.token_usage.cost_usd)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["invalid_tool_arguments", "bad_response", "server_error"])
async def test_the_loop_records_the_adapter_error_code_in_the_failure_reason(code: str) -> None:
    """The code has to reach the trace, or the taxonomy cannot classify by it.

    `ModelAdapterError` covers both a provider outage and a model that produced an
    unusable response.  The failure taxonomy separates them by code, so a reason that
    keeps only the class name forces it back onto message matching -- which is what it
    did, and what filed a malformed model response under `environment`.
    """
    loop = AgentLoop(
        model=CodedFailureModel(code=code),
        context_manager=FullContext(),
        tools=FakeTools(),
        events=FakeEvents(),
    )

    result = await loop.run("do something")

    assert result.status is RunStatus.FAILED
    assert result.failure_reason is not None
    assert result.failure_reason.startswith(f"ModelAdapterError[{code}]:")


@dataclass
class TimedTools:
    """Tool executor whose observations carry a configurable wall-clock duration."""

    duration_ms: float

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ToolDefinition("read_file", "read", {"type": "object"}),)

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        del run_id, step_id
        return ToolObservation(
            call.call_id,
            call.name,
            ToolStatus.SUCCEEDED,
            "contents",
            duration_ms=self.duration_ms,
        )


async def _rendered_feedback(duration_ms: float) -> str:
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(call,), usage=TokenUsage(10, 5)),
            ModelResponse(content="done", usage=TokenUsage(20, 3)),
        ]
    )
    loop = AgentLoop(
        model=model,
        context_manager=FullContext(),
        tools=TimedTools(duration_ms),
        events=FakeEvents(),
    )

    await loop.run("inspect the repository", run_id="run-1")

    return model.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_model_visible_feedback_is_deterministic() -> None:
    """Wall-clock timing must not leak into the prompt, or token counts drift per run."""
    fast = await _rendered_feedback(1.5)
    slow = await _rendered_feedback(987.654321)

    assert fast == slow
    assert "duration" not in fast
    assert fast == json.dumps(
        {
            "artifacts": [],
            "result": "contents",
            "retryable": False,
            "status": "succeeded",
            "truncated": False,
        },
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_a_failed_model_call_is_charged_to_the_model_not_the_harness() -> None:
    """A provider outage must not be reported as harness overhead.

    The split exists to answer "how much of this is my code".  If a call that
    spends time inside the provider and then fails is left unrecorded, that time
    lands in the harness residual and the harness looks slow exactly when the
    provider was at fault.
    """
    model = FailingModel(delay_seconds=0.05)
    loop = AgentLoop(
        model=model,
        context_manager=FullContext(),
        tools=FakeTools(),
        events=FakeEvents(),
    )

    result = await loop.run("do something", run_id="run-fail")

    assert result.status is RunStatus.FAILED
    assert result.latency.model_calls == 1, "a failed call is still a call that was made"
    assert result.latency.model_ms >= 40.0, "the provider's 50ms must be attributed to it"
    assert result.latency.harness_ms < result.latency.model_ms, (
        f"harness {result.latency.harness_ms:.1f}ms absorbed the provider's time "
        f"instead of model {result.latency.model_ms:.1f}ms"
    )

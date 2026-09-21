"""Contract tests for the core agent loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from minidsh.application import AgentLoop
from minidsh.domain import (
    AgentBudget,
    ContextView,
    Event,
    Message,
    ModelRequest,
    ModelResponse,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)


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
    assert [event.name for event in events.events] == [
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

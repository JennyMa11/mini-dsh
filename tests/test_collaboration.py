"""Contracts for bounded Sub Agent collaboration and shared accounting."""

import asyncio
import json
from pathlib import Path

import pytest

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.collaboration import build_multi_agent_runtime
from minidsh.composition import RuntimeOptions
from minidsh.domain import (
    AgentBudget,
    Event,
    JsonValue,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolStatus,
)
from minidsh.evaluation import TraceRun, build_replay


def _delegation(*tasks: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=(
            ToolCall(
                "delegate",
                "delegate_sub_agents",
                {
                    "tasks": [
                        {"name": f"agent_{index}", "task": task, "role": "reviewer"}
                        for index, task in enumerate(tasks)
                    ],
                },
            ),
        )
    )


@pytest.mark.asyncio
async def test_parent_collects_children_with_independent_runs_and_total_cost(
    tmp_path: Path,
) -> None:
    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel(
            [
                _delegation("inspect", "review"),
                ModelResponse("combined", usage=TokenUsage(7, 3, cached_input_tokens=2)),
            ]
        ),
        subagent_model_factory=lambda: ScriptedModel(
            [ModelResponse("child answer", usage=TokenUsage(10, 5, cached_input_tokens=4))]
        ),
        options=RuntimeOptions(pricing=ModelPricing(2, 4, 1)),
    )
    trace_path = tmp_path / "trace.jsonl"
    runtime.events.subscribe("*", JsonlTraceWriter(trace_path))
    events: list[Event] = []

    async def record(event: Event) -> None:
        events.append(event)

    runtime.events.subscribe("*", record)
    result = await runtime.agent.run("parent task", run_id="parent")
    assert result.status is RunStatus.FINISHED
    assert result.answer == "combined"
    observations = [message for message in result.history if message.name == "delegate_sub_agents"]
    children = json.loads(observations[0].content)["result"]["agents"]
    assert len({child["run_id"] for child in children}) == 2
    assert all(child["answer"] == "child answer" for child in children)
    assert result.token_usage.total_tokens == 40
    assert result.token_usage.cached_input_tokens == 10
    assert result.cost_usd == pytest.approx(0.000096)
    trace = TraceRun.from_file(trace_path)
    assert trace.status() == "finished"
    assert len(trace.steps()) == 2
    assert trace.total_tokens() == (27, 13, 10)
    assert trace.cost_usd() == pytest.approx(result.cost_usd)
    assert build_replay(trace).tokens == (27, 13, 10)
    child_trace = TraceRun.from_file(trace_path, run_id=children[0]["run_id"])
    assert child_trace.status() == "finished"
    assert len(child_trace.steps()) == 1
    assert await runtime.sessions.load("parent") == result
    linked = [event for event in events if event.name == "sub_agent_started"]
    assert len(linked) == 2
    assert all(event.run_id == "parent" for event in linked)
    assert all(
        event.data.get("parent_run_id") == "parent" for event in events if event.run_id != "parent"
    )


@pytest.mark.asyncio
async def test_children_cannot_write_or_delegate_recursively(tmp_path: Path) -> None:
    seen: list[set[str]] = []

    class ChildModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append({tool.name for tool in request.tools})
            return ModelResponse("read only")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("done")]),
        subagent_model_factory=ChildModel,
        options=RuntimeOptions(allow_write=True, allow_shell=True),
    )
    await runtime.agent.run("task")
    assert seen == [{"read_file", "search", "git"}]
    assert "write_file" in {tool.name for tool in runtime.tools.definitions()}


@pytest.mark.asyncio
async def test_shared_step_budget_cannot_be_bypassed_by_children(tmp_path: Path) -> None:
    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect", "review"), ModelResponse("done")]),
        subagent_model_factory=lambda: ScriptedModel([ModelResponse("answer")]),
        options=RuntimeOptions(),
        budget=AgentBudget(max_steps=2),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "team step budget exhausted"


@pytest.mark.asyncio
async def test_denied_delegation_does_not_construct_a_child(tmp_path: Path) -> None:
    def factory() -> ScriptedModel:
        pytest.fail("a denied tool must not create an agent")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        subagent_model_factory=factory,
        options=RuntimeOptions(denied_tools=frozenset({"delegate_sub_agents"})),
    )
    result = await runtime.tools.execute(
        ToolCall("c", "delegate_sub_agents", {"tasks": [{"name": "a", "task": "inspect"}]}),
        "parent",
        1,
    )
    assert result.status is ToolStatus.DENIED


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_results_preserve_input_order(tmp_path: Path) -> None:
    active = 0
    peak = 0
    barrier = asyncio.Event()

    class BlockingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                barrier.set()
            await barrier.wait()
            active -= 1
            return ModelResponse(request.messages[-1].content)

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("one", "two", "three"), ModelResponse("done")]),
        subagent_model_factory=BlockingModel,
        options=RuntimeOptions(subagent_max_parallel=2),
    )
    result = await asyncio.wait_for(runtime.agent.run("task"), timeout=2)
    assert result.status is RunStatus.FINISHED
    assert peak == 2
    observation = next(
        message for message in result.history if message.name == "delegate_sub_agents"
    )
    assert [item["answer"] for item in json.loads(observation.content)["result"]["agents"]] == [
        "one",
        "two",
        "three",
    ]


@pytest.mark.asyncio
async def test_parent_cancellation_joins_children(tmp_path: Path) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    class BlockingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return ModelResponse("unreachable")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect")]),
        subagent_model_factory=BlockingModel,
        options=RuntimeOptions(),
    )
    task = asyncio.create_task(runtime.agent.run("task", run_id="cancelled_parent"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    saved = await runtime.sessions.load("cancelled_parent")
    assert saved is not None
    assert saved.status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_shared_token_budget_includes_child_tokens(tmp_path: Path) -> None:
    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("unreachable")]),
        subagent_model_factory=lambda: ScriptedModel(
            [ModelResponse("answer", usage=TokenUsage(100, 100))]
        ),
        options=RuntimeOptions(),
        budget=AgentBudget(max_tokens=150),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "team token budget exhausted"
    assert result.token_usage.total_tokens >= 200


@pytest.mark.asyncio
async def test_shared_tool_budget_stops_child_tool_calls(tmp_path: Path) -> None:
    child = ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "missing"}),))
    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("unreachable")]),
        subagent_model_factory=lambda: ScriptedModel([child, ModelResponse("unreachable")]),
        options=RuntimeOptions(),
        budget=AgentBudget(max_tool_calls=1),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "team tool-call budget exhausted"
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_cumulative_agent_limit_is_not_reset_between_batches(tmp_path: Path) -> None:
    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("first"), _delegation("second"), ModelResponse("done")]),
        subagent_model_factory=lambda: ScriptedModel([ModelResponse("answer")]),
        options=RuntimeOptions(subagent_max_agents=1),
    )
    result = await runtime.agent.run("task")
    observations = [
        json.loads(message.content)
        for message in result.history
        if message.name == "delegate_sub_agents"
    ]
    assert observations[0]["status"] == "succeeded"
    assert observations[1]["error"]["code"] == "subagent_limit"


@pytest.mark.asyncio
async def test_failed_child_is_a_structured_result_and_parent_can_continue(tmp_path: Path) -> None:
    def factory() -> ScriptedModel:
        raise RuntimeError("secret-containing internal details must not leak")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("recovered")]),
        subagent_model_factory=factory,
        options=RuntimeOptions(),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    observation = next(
        message for message in result.history if message.name == "delegate_sub_agents"
    )
    child = json.loads(observation.content)["result"]["agents"][0]
    assert child["status"] == "failed"
    assert child["failure_reason"] == "Sub Agent startup failed"


@pytest.mark.asyncio
async def test_child_timeout_stops_work_and_parent_can_continue(tmp_path: Path) -> None:
    stopped = asyncio.Event()

    class BlockingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return ModelResponse("unreachable")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("done")]),
        subagent_model_factory=BlockingModel,
        options=RuntimeOptions(subagent_max_seconds=0.02),
    )
    trace_path = tmp_path / "timeout.jsonl"
    runtime.events.subscribe("*", JsonlTraceWriter(trace_path))
    result = await asyncio.wait_for(runtime.agent.run("task"), timeout=2)
    assert result.status is RunStatus.FINISHED
    assert stopped.is_set()
    observation = next(
        message for message in result.history if message.name == "delegate_sub_agents"
    )
    child = json.loads(observation.content)["result"]["agents"][0]
    assert child["status"] == "budget_exhausted"
    assert TraceRun.from_file(trace_path, run_id=child["run_id"]).status() == "budget_exhausted"
    saved = await runtime.sessions.load(child["run_id"])
    assert saved is not None
    assert saved.status is RunStatus.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_team_deadline_cancels_children_and_exhausts_parent(tmp_path: Path) -> None:
    stopped = asyncio.Event()

    class BlockingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return ModelResponse("unreachable")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([_delegation("inspect"), ModelResponse("done")]),
        subagent_model_factory=BlockingModel,
        options=RuntimeOptions(),
        budget=AgentBudget(max_duration_seconds=0.02),
    )
    result = await asyncio.wait_for(runtime.agent.run("task"), timeout=2)
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "team duration budget exhausted"
    assert stopped.is_set()


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [{"name": "a", "task": "one"}, {"name": "a", "task": "two"}],
        [{"name": "../escape", "task": "inspect"}],
        [{"name": "a", "task": "inspect", "allow_write": True}],
    ],
)
@pytest.mark.asyncio
async def test_invalid_delegation_never_starts_children(
    tmp_path: Path, tasks: list[JsonValue]
) -> None:
    def factory() -> ScriptedModel:
        pytest.fail("invalid delegation must not construct a child")

    runtime = build_multi_agent_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        subagent_model_factory=factory,
        options=RuntimeOptions(),
    )
    observation = await runtime.tools.execute(
        ToolCall("invalid", "delegate_sub_agents", {"tasks": tasks}), "parent", 1
    )
    assert observation.status is ToolStatus.INVALID

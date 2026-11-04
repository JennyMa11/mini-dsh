"""Optional bounded Sub Agent collaboration around the single-agent application."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minidsh.adapters import JsonSessionStore
from minidsh.composition import LocalRuntime, RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import (
    AgentBudget,
    ErrorInfo,
    Event,
    JsonValue,
    Message,
    ModelCallError,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.infrastructure import NetworkPolicy
from minidsh.ports import AgentRunner, EventStream, Model, SessionStore, ToolExecutor
from minidsh.runtime import EventBus
from minidsh.tools import ToolError


class _TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    task: str = Field(min_length=1, max_length=16_000)
    role: str = Field(default="analyst", min_length=1, max_length=128)


class _DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[_TaskInput] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        if len({task.name for task in self.tasks}) != len(self.tasks):
            raise ValueError("subagent names must be unique within a batch")
        return self


class _TeamBudgetError(RuntimeError):
    """An attempted operation exceeded a shared team budget."""


@dataclass(slots=True)
class _TeamState:
    run_id: str
    budget: AgentBudget
    parallel: int
    started: float = field(default_factory=time.monotonic)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model_calls: int = 0
    tool_calls: int = 0
    agents: int = 0
    exhausted: str | None = None
    semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.parallel)

    def remaining_seconds(self) -> float:
        return self.budget.max_duration_seconds - (time.monotonic() - self.started)

    def check(self) -> None:
        if self.remaining_seconds() <= 0:
            self.exhausted = "team duration budget exhausted"
        if self.exhausted is not None:
            raise _TeamBudgetError(self.exhausted)

    def reserve_model(self) -> None:
        self.check()
        if self.model_calls >= self.budget.max_steps:
            self.exhausted = "team step budget exhausted"
            raise _TeamBudgetError(self.exhausted)
        self.model_calls += 1

    def reserve_tool(self) -> None:
        self.check()
        if self.tool_calls >= self.budget.max_tool_calls:
            self.exhausted = "team tool-call budget exhausted"
            raise _TeamBudgetError(self.exhausted)
        self.tool_calls += 1

    def aggregate(self, result: RunResult) -> RunResult:
        if result.run_id != self.run_id:
            return result
        exhausted = self.exhausted
        return replace(
            result,
            token_usage=self.usage,
            tool_calls=self.tool_calls,
            status=(
                RunStatus.BUDGET_EXHAUSTED
                if exhausted and result.status is not RunStatus.CANCELLED
                else result.status
            ),
            failure_reason=(
                exhausted
                if exhausted and result.status is not RunStatus.CANCELLED
                else result.failure_reason
            ),
        )


@dataclass(frozen=True, slots=True)
class _CallScope:
    run_id: str
    step_id: int
    call_id: str


@dataclass(frozen=True, slots=True)
class _ChildScope:
    run_id: str
    deadline: float


class _Coordinator:
    def __init__(
        self, options: RuntimeOptions, budget: AgentBudget, child_pricing: ModelPricing
    ) -> None:
        self.options = options
        self.budget = budget
        self.child_pricing = child_pricing
        self.run_lock = asyncio.Lock()
        self.state: ContextVar[_TeamState | None] = ContextVar("minidsh_team", default=None)
        self.call: ContextVar[_CallScope | None] = ContextVar("minidsh_team_call", default=None)
        self.child: ContextVar[_ChildScope | None] = ContextVar("minidsh_child", default=None)

    def current(self) -> _TeamState:
        state = self.state.get()
        if state is None:
            raise ToolError("no_active_team", "delegation requires an active parent run")
        return state

    def terminal_reason(self, run_id: str, status: RunStatus) -> str | None:
        child = self.child.get()
        if child is not None and child.run_id == run_id and time.monotonic() >= child.deadline:
            return "subagent duration budget exhausted"
        state = self.state.get()
        if state is not None and status is not RunStatus.CANCELLED:
            return state.exhausted
        return None

    def normalize(self, result: RunResult) -> RunResult:
        state = self.state.get()
        if state is not None:
            result = state.aggregate(result)
        reason = self.terminal_reason(result.run_id, result.status)
        return (
            replace(result, status=RunStatus.BUDGET_EXHAUSTED, failure_reason=reason)
            if reason
            else result
        )


class _BudgetModel:
    def __init__(self, model: Model, coordinator: _Coordinator, pricing: ModelPricing) -> None:
        self._model = model
        self._coordinator = coordinator
        self._pricing = pricing

    async def complete(self, request: ModelRequest) -> ModelResponse:
        state = self._coordinator.current()
        state.reserve_model()
        try:
            async with asyncio.timeout(state.remaining_seconds()):
                response = await self._model.complete(request)
        except TimeoutError as exc:
            state.exhausted = "team duration budget exhausted"
            raise _TeamBudgetError(state.exhausted) from exc
        except ModelCallError as exc:
            if exc.usage is not None:
                self._charge(state, exc.usage)
            raise
        self._charge(state, response.usage)
        return response

    def _charge(self, state: _TeamState, usage: TokenUsage) -> None:
        state.usage += replace(usage, cost_usd=self._pricing.cost_usd(usage))
        if state.usage.total_tokens >= state.budget.max_tokens:
            state.exhausted = "team token budget exhausted"


class _BudgetTools:
    def __init__(self, tools: ToolExecutor, coordinator: _Coordinator, events: EventStream) -> None:
        self._tools = tools
        self._coordinator = coordinator
        self._events = events

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._tools.definitions()

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        state = self._coordinator.state.get()
        if state is None:
            return await self._tools.execute(call, run_id, step_id)
        try:
            state.reserve_tool()
        except _TeamBudgetError:
            return await self._budget_observation(call, run_id, step_id, state)
        token = self._coordinator.call.set(_CallScope(run_id, step_id, call.call_id))
        try:
            async with asyncio.timeout(state.remaining_seconds()):
                return await self._tools.execute(call, run_id, step_id)
        except TimeoutError:
            state.exhausted = "team duration budget exhausted"
            return await self._budget_observation(call, run_id, step_id, state)
        finally:
            self._coordinator.call.reset(token)

    async def _budget_observation(
        self, call: ToolCall, run_id: str, step_id: int, state: _TeamState
    ) -> ToolObservation:
        observation = ToolObservation(
            call.call_id,
            call.name,
            ToolStatus.DENIED,
            error=ErrorInfo("team_budget_exhausted", state.exhausted or "team budget exhausted"),
        )
        await self._events.publish(
            Event(
                "tool_call_completed",
                run_id,
                step_id,
                {
                    "tool": call.name,
                    "call_id": call.call_id,
                    "status": observation.status.value,
                    "error": {"code": "team_budget_exhausted", "message": state.exhausted},
                    "duration_ms": 0.0,
                    "truncated": False,
                    "retryable": False,
                    "attempt": 0,
                },
            )
        )
        return observation


class _TeamEvents:
    def __init__(self, events: EventStream, coordinator: _Coordinator) -> None:
        self._events = events
        self._coordinator = coordinator

    def subscribe(
        self, event_name: str, handler: Callable[[Event], Awaitable[None]]
    ) -> Callable[[], None]:
        return self._events.subscribe(event_name, handler)

    async def publish(self, event: Event) -> None:
        state = self._coordinator.state.get()
        if state is not None:
            data = dict(event.data)
            if event.run_id != state.run_id:
                data["parent_run_id"] = state.run_id
                if event.name == "run_finished":
                    reason = self._coordinator.terminal_reason(
                        event.run_id, RunStatus(str(data["status"]))
                    )
                    if reason:
                        data.update(
                            {"status": RunStatus.BUDGET_EXHAUSTED.value, "failure_reason": reason}
                        )
            elif event.name == "run_finished":
                if state.remaining_seconds() <= 0:
                    state.exhausted = "team duration budget exhausted"
                data.update(
                    {
                        "tokens": state.usage.total_tokens,
                        "input_tokens": state.usage.input_tokens,
                        "output_tokens": state.usage.output_tokens,
                        "cost_usd": state.usage.cost_usd,
                        "cached_input_tokens": state.usage.cached_input_tokens,
                        "tool_calls": state.tool_calls,
                        "team_steps": state.model_calls,
                        "sub_agents": state.agents,
                    }
                )
                if state.exhausted and data["status"] != RunStatus.CANCELLED.value:
                    data.update(
                        {
                            "status": RunStatus.BUDGET_EXHAUSTED.value,
                            "failure_reason": state.exhausted,
                        }
                    )
            event = replace(event, data=data)
        await self._events.publish(event)


class _TeamSessions:
    def __init__(self, sessions: SessionStore, coordinator: _Coordinator) -> None:
        self._sessions = sessions
        self._coordinator = coordinator

    async def save(self, result: RunResult) -> None:
        await self._sessions.save(self._coordinator.normalize(result))

    async def load(self, run_id: str) -> RunResult | None:
        return await self._sessions.load(run_id)


class _TeamAgent:
    def __init__(self, agent: AgentRunner, coordinator: _Coordinator) -> None:
        self._agent = agent
        self._coordinator = coordinator

    async def run(
        self, task: str, *, run_id: str | None = None, prior_history: tuple[Message, ...] = ()
    ) -> RunResult:
        async with self._coordinator.run_lock:
            return await self._run(task, run_id=run_id, prior_history=prior_history)

    async def _run(
        self, task: str, *, run_id: str | None, prior_history: tuple[Message, ...]
    ) -> RunResult:
        state = _TeamState(
            run_id or uuid.uuid4().hex,
            self._coordinator.budget,
            self._coordinator.options.subagent_max_parallel,
        )
        token = self._coordinator.state.set(state)
        try:
            result = await self._agent.run(task, run_id=state.run_id, prior_history=prior_history)
            return state.aggregate(result)
        finally:
            self._coordinator.state.reset(token)


class _DelegateTool:
    input_model = _DelegateInput

    def __init__(
        self,
        workspace: Path,
        factory: Callable[[], Model],
        coordinator: _Coordinator,
        events: EventStream,
        sessions: SessionStore,
    ) -> None:
        self._workspace = workspace
        self._factory = factory
        self._coordinator = coordinator
        self._events = events
        self._sessions = sessions
        self.definition = ToolDefinition(
            "delegate_sub_agents",
            "Delegate independent read-only tasks to Sub Agents and collect their findings. "
            "Provide a unique name, task, and optional role for each agent; "
            "implement changes yourself.",
            _DelegateInput.model_json_schema(),
            side_effects=True,
            permissions=("agent.delegate",),
            approval_required=False,
            timeout_seconds=coordinator.budget.max_duration_seconds,
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        assert isinstance(arguments, _DelegateInput)
        state = self._coordinator.current()
        scope = self._coordinator.call.get()
        if scope is None or scope.run_id != state.run_id:
            raise ToolError("invalid_parent", "only the active parent agent may delegate")
        state.check()
        if state.agents + len(arguments.tasks) > self._coordinator.options.subagent_max_agents:
            raise ToolError("subagent_limit", "cumulative Sub Agent limit exceeded")
        state.agents += len(arguments.tasks)
        tasks = [
            asyncio.create_task(self._run_child(item, state, scope)) for item in arguments.tasks
        ]
        try:
            results = await asyncio.gather(*tasks)
            return {"agents": results}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_child(self, item: _TaskInput, state: _TeamState, scope: _CallScope) -> JsonValue:
        child_id = uuid.uuid4().hex
        metadata: dict[str, JsonValue] = {
            "child_run_id": child_id,
            "name": item.name,
            "role": item.role,
            "call_id": scope.call_id,
        }
        async with state.semaphore:
            await self._events.publish(
                Event("sub_agent_started", state.run_id, scope.step_id, metadata)
            )
            try:
                state.check()
                result = await self._execute_child(item, child_id)
                payload: dict[str, JsonValue] = {
                    "name": item.name,
                    "role": item.role,
                    "run_id": child_id,
                    "status": result.status.value,
                    "answer": result.answer,
                    "failure_reason": result.failure_reason,
                    "tokens": result.token_usage.total_tokens,
                    "cost_usd": result.cost_usd,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                }
            except asyncio.CancelledError:
                await self._events.publish(
                    Event(
                        "sub_agent_finished",
                        state.run_id,
                        scope.step_id,
                        {**metadata, "status": "cancelled"},
                    )
                )
                raise
            except _TeamBudgetError:
                payload = {
                    "name": item.name,
                    "role": item.role,
                    "run_id": child_id,
                    "status": "budget_exhausted",
                    "answer": "",
                    "failure_reason": state.exhausted,
                }
            except Exception:
                payload = {
                    "name": item.name,
                    "role": item.role,
                    "run_id": child_id,
                    "status": "failed",
                    "answer": "",
                    "failure_reason": "Sub Agent startup failed",
                }
            await self._events.publish(
                Event(
                    "sub_agent_finished",
                    state.run_id,
                    scope.step_id,
                    {**metadata, "status": payload["status"]},
                )
            )
            return payload

    async def _execute_child(self, item: _TaskInput, child_id: str) -> RunResult:
        options = self._coordinator.options
        child_options = replace(
            options,
            multi_agent_enabled=False,
            allow_write=False,
            allow_shell=False,
            builtin_tools_enabled=True,
            enabled_tools=frozenset({"read_file", "search", "git"}),
            extra_permissions=frozenset(),
            network=NetworkPolicy.deny_all(),
            pricing=self._coordinator.child_pricing,
            system_prompt=options.system_prompt
            + f"\nYou are a read-only Sub Agent with role: {item.role}. "
            "Complete your assigned task and report findings to the parent. "
            "Do not edit files or delegate further tasks.",
        )
        budget = AgentBudget(
            options.subagent_max_steps,
            options.subagent_max_tokens,
            options.subagent_max_tool_calls,
            options.subagent_max_seconds,
        )
        runtime = build_local_runtime(
            workspace=self._workspace,
            model=_BudgetModel(self._factory(), self._coordinator, child_options.pricing),
            options=child_options,
            budget=budget,
            events=self._events,
            components=RuntimeComponents(
                sessions=self._sessions,
                tool_wrapper=lambda tools: _BudgetTools(tools, self._coordinator, self._events),
            ),
        )
        token = self._coordinator.child.set(
            _ChildScope(child_id, time.monotonic() + options.subagent_max_seconds)
        )
        try:
            async with asyncio.timeout(options.subagent_max_seconds):
                result = await runtime.agent.run(item.task, run_id=child_id)
                return self._coordinator.normalize(result)
        except TimeoutError:
            saved = await self._sessions.load(child_id)
            if saved is None:
                raise RuntimeError("cancelled child session missing") from None
            result = replace(
                saved,
                status=RunStatus.BUDGET_EXHAUSTED,
                failure_reason="subagent duration budget exhausted",
            )
            await self._sessions.save(result)
            return result
        finally:
            self._coordinator.child.reset(token)


def build_multi_agent_runtime(  # noqa: PLR0913 - explicit composition dependencies
    *,
    workspace: Path,
    model: Model,
    subagent_model_factory: Callable[[], Model],
    options: RuntimeOptions,
    budget: AgentBudget | None = None,
    events: EventStream | None = None,
    subagent_pricing: ModelPricing | None = None,
) -> LocalRuntime:
    """Compose an opt-in team; each factory call must return an independent model adapter."""
    actual_budget = budget or AgentBudget()
    coordinator = _Coordinator(options, actual_budget, subagent_pricing or options.pricing)
    bus = _TeamEvents(events if events is not None else EventBus(), coordinator)
    sessions = _TeamSessions(JsonSessionStore(workspace), coordinator)
    tool = _DelegateTool(workspace, subagent_model_factory, coordinator, bus, sessions)
    team_options = replace(
        options,
        multi_agent_enabled=False,
        extra_permissions=options.extra_permissions | {"agent.delegate"},
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=_BudgetModel(model, coordinator, options.pricing),
        options=team_options,
        budget=actual_budget,
        events=bus,
        components=RuntimeComponents(
            sessions=sessions,
            tools=(tool,),
            tool_wrapper=lambda tools: _BudgetTools(tools, coordinator, bus),
        ),
    )
    return replace(runtime, agent=_TeamAgent(runtime.agent, coordinator), conversations=None)

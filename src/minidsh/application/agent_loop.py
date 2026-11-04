"""The provider-agnostic Observe/Plan/Act/Execute agent loop."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace

from minidsh.application.local_policy import LocalPlanState, RepetitionGuard
from minidsh.application.session_journal import MemorySessionJournal
from minidsh.domain import (
    AgentBudget,
    Event,
    JsonValue,
    LatencyBreakdown,
    Message,
    ModelCallError,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    Role,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolObservation,
    estimate_tokens,
    redact_text,
)
from minidsh.domain.extensions import (
    BudgetView,
    ExtensionBudgetExhaustedError,
    FinishAction,
    FinishProposal,
    HookView,
)
from minidsh.domain.harness import StreamEvent
from minidsh.ports import (
    AgentDependencies,
    AgentRunner,
    ContextManager,
    EventSink,
    ExecutionHooks,
    Model,
    SessionJournal,
    SessionLog,
    SessionStore,
    StreamingModel,
    ToolExecutor,
)
from minidsh.ports.protocols import ExtensionHooks, PreparedContextManager

_MAX_FORMAT_FAILURES = 3
_MAX_REJECTED_FINISHES = 3
_UNRESOLVED_EDIT_REASON = "unresolved_edit: no successful edit after patch failure"


async def _close_stream(stream: AsyncIterator[StreamEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if callable(close):
        await close()


def _failure_reason(exc: BaseException) -> str:
    """Render an exception for the trace, preserving any error code it carries.

    The code is part of the record rather than decoration: it is what lets the
    failure taxonomy tell a provider outage apart from a model that produced an
    unusable response, without matching on message text.  `ModelAdapterError` covers
    both, so a reason carrying only the class name cannot be classified correctly.

    Read by attribute rather than by importing the adapter's exception class: the
    loop depends on the `Model` port, not on any adapter.
    """
    phase = getattr(exc, "_extension_phase", None)
    if isinstance(phase, str) and phase in {
        "before_request",
        "before_tool",
        "after_tool",
        "legacy_before_finish",
    }:
        return f"extension_hook_failed[{phase}]: {type(exc).__name__[:128]}"
    code = getattr(exc, "code", None)
    label = type(exc).__name__
    if isinstance(code, str) and code:
        label = f"{label}[{code}]"
    return f"{label}: {exc}"


@dataclass(slots=True)
class _RunCounters:
    steps: int = 0
    closed_step: int = 0
    policy_failure: str | None = None
    legacy_finished: bool = False
    cleanup_deadline: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    model_calls: int = 0
    format_failures: int = 0
    rejected_finishes: int = 0
    unknown_usage_calls: int = 0

    @property
    def token_usage(self) -> TokenUsage:
        return TokenUsage(
            self.input_tokens,
            self.output_tokens,
            self.cost_usd,
            self.cached_input_tokens,
        )

    def latency(self, total_ms: float) -> LatencyBreakdown:
        """Attribute end-to-end latency to model, tools, and harness residual."""
        observed = self.model_ms + self.tool_ms
        return LatencyBreakdown(
            model_ms=self.model_ms,
            tool_ms=self.tool_ms,
            harness_ms=max(0.0, total_ms - observed),
            total_ms=total_ms,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
        )


@dataclass(slots=True)
class _RunCaller:
    """A phase-scoped capability; neither a raw executor nor shared run state."""

    _action: Callable[[str, dict[str, JsonValue]], Awaitable[ToolObservation]]
    _view: Callable[[], HookView]
    _owner: int = field(default_factory=lambda: id(asyncio.current_task()))
    _active: bool = True
    _inflight: bool = False

    def close(self) -> None:
        self._active = False

    def _check(self) -> None:
        if not self._active:
            raise RuntimeError("extension caller is inactive")
        if id(asyncio.current_task()) != self._owner:
            raise RuntimeError("extension caller requires its owning task")
        if self._inflight:
            raise RuntimeError("recursive extension tool invocation")

    def snapshot(self) -> HookView:
        self._check()
        return self._view()

    async def invoke(self, name: str, arguments: dict[str, JsonValue]) -> ToolObservation:
        self._check()
        self._inflight = True
        try:
            return await self._action(name, arguments)
        finally:
            self._inflight = False


class AgentLoop:
    """Coordinate model, context, tools, and events through explicit ports."""

    def __init__(  # noqa: PLR0913 - explicit injected ports are the composition boundary
        self,
        *,
        model: Model,
        context_manager: ContextManager,
        tools: ToolExecutor,
        events: EventSink,
        budget: AgentBudget | None = None,
        pricing: ModelPricing | None = None,
        system_prompt: str = "You are a coding agent. Use tools to complete the task.",
        reflection_enabled: bool = False,
        detailed_tool_feedback: bool = True,
        sessions: SessionStore | None = None,
        journal: SessionJournal | None = None,
        local_first_enabled: bool = False,
        error_recovery_enabled: bool = True,
        workspace_instructions_enabled: bool = False,
        streaming_enabled: bool = False,
        hooks: ExecutionHooks | None = None,
        request_input_budget: int | None = None,
    ) -> None:
        self._model = model
        self._context = context_manager
        self._tools = tools
        self._events = events
        self._budget = budget or AgentBudget()
        self._pricing = pricing or ModelPricing.free()
        self._system_prompt = system_prompt
        self._reflection_enabled = reflection_enabled
        self._detailed_tool_feedback = detailed_tool_feedback
        self._sessions = sessions
        self._journal = journal if journal is not None else MemorySessionJournal()
        self._local_first_enabled = local_first_enabled
        self._error_recovery_enabled = error_recovery_enabled
        self._workspace_instructions_enabled = workspace_instructions_enabled
        self._streaming_enabled = streaming_enabled
        self._hooks = hooks
        self._extensions = hooks if isinstance(hooks, ExtensionHooks) else None
        self._request_input_budget = request_input_budget

    async def run(  # noqa: PLR0911, PLR0912, PLR0915 - explicit state transitions remain visible
        self, task: str, *, run_id: str | None = None, prior_history: tuple[Message, ...] = ()
    ) -> RunResult:
        """Run a new budgeted turn, optionally continuing an earlier immutable history."""
        actual_run_id = run_id or uuid.uuid4().hex
        started = time.monotonic()
        counters = _RunCounters()
        seed, repaired = self._continue_history(prior_history)
        log = await self._journal.create(actual_run_id)
        plan = (
            LocalPlanState(task, self._error_recovery_enabled)
            if self._local_first_enabled
            else None
        )
        guard = RepetitionGuard(self._error_recovery_enabled) if self._local_first_enabled else None
        await self._emit("run_started", actual_run_id, 0, {"task_length": len(task)})
        if prior_history:
            await self._emit(
                "conversation_restored",
                actual_run_id,
                0,
                {"message_count": len(prior_history), "interrupted_calls": repaired},
            )

        try:
            committed = log.messages()
            if committed and committed[0] != seed[0]:
                await self._append(
                    log,
                    actual_run_id,
                    0,
                    Message(Role.SYSTEM, self._system_prompt, name="system_prompt_updated"),
                )
            initial = seed[len(committed) :] if committed else seed
            for message in (*initial, Message(Role.USER, task)):
                await self._append(log, actual_run_id, 0, message)
            if self._extensions is not None:
                await self._extensions.started(
                    self._hook_view(actual_run_id, counters, started, log)
                )
            if self._workspace_instructions_enabled:
                await self._prepare_workspace(log, counters, actual_run_id, started)
            if plan is not None:
                await self._append(
                    log, actual_run_id, 0, Message(Role.SYSTEM, plan.render(), name="plan_state")
                )
            while True:
                await self._end_step(actual_run_id, counters, started, log)
                exhausted = self._budget_reason(counters, started)
                if exhausted is not None:
                    return await self._finish(
                        actual_run_id,
                        RunStatus.BUDGET_EXHAUSTED,
                        log,
                        counters,
                        started,
                        failure_reason=exhausted,
                    )

                counters.steps += 1
                step_id = counters.steps
                if isinstance(self._context, PreparedContextManager):

                    def account_context(calls: int, duration: float) -> None:
                        counters.tool_calls += calls
                        counters.tool_ms += duration

                    with (
                        self._extensions.tool_scope(
                            self._hook_view(actual_run_id, counters, started, log)
                        )
                        if self._extensions
                        else nullcontext()
                    ):
                        async with asyncio.timeout(
                            max(
                                0.0,
                                self._budget.max_duration_seconds - (time.monotonic() - started),
                            )
                        ):
                            prepared = await self._context.prepare(
                                log.messages(),
                                task,
                                actual_run_id,
                                step_id,
                                self._budget.max_tool_calls - counters.tool_calls,
                                account_context,
                            )
                    context = prepared.view
                    if (
                        counters.tool_calls >= self._budget.max_tool_calls
                        or time.monotonic() - started >= self._budget.max_duration_seconds
                    ):
                        continue
                else:
                    context = self._context.build(log.messages(), task)
                await self._emit(
                    "context_built",
                    actual_run_id,
                    step_id,
                    {
                        "strategy": context.strategy,
                        "version": context.version,
                        "estimated_tokens": context.estimated_tokens,
                        "omitted_messages": context.omitted_messages,
                        "metadata": context.metadata,
                    },
                )
                request = ModelRequest(context.messages, self._tools.definitions())
                async with asyncio.timeout(
                    max(0.0, self._budget.max_duration_seconds - (time.monotonic() - started))
                ):
                    with (
                        self._extensions.tool_scope(
                            self._hook_view(actual_run_id, counters, started, log)
                        )
                        if self._extensions
                        else nullcontext()
                    ):
                        rewritten = await self._hooks.request(request) if self._hooks else request
                        if self._extensions is not None:
                            rewritten = await self._extensions.model(
                                rewritten, self._hook_view(actual_run_id, counters, started, log)
                            )
                if time.monotonic() - started >= self._budget.max_duration_seconds:
                    continue
                count = getattr(self._model, "count_tokens", None)
                estimated = (
                    count(rewritten.messages, rewritten.tools)
                    if callable(count)
                    else estimate_tokens(rewritten.messages)
                )
                if (
                    self._request_input_budget is not None
                    and estimated > self._request_input_budget
                ):
                    raise ValueError("rewritten request exceeds context input budget")
                if rewritten != request:
                    await self._emit(
                        "model_request_rewritten",
                        actual_run_id,
                        step_id,
                        {
                            "before_messages": [
                                self._message_json(message) for message in request.messages
                            ],
                            "after_messages": [
                                self._message_json(message) for message in rewritten.messages
                            ],
                        },
                    )
                    request = rewritten
                await self._emit(
                    "model_call_started",
                    actual_run_id,
                    step_id,
                    {
                        "messages": [self._message_json(message) for message in request.messages],
                        "tools": [
                            {"name": tool.name, "version": tool.version} for tool in request.tools
                        ],
                    },
                )
                response, model_duration_ms = await self._call_model(
                    request, counters, actual_run_id, step_id, started
                )
                call_usage = response.usage
                if not response.usage_known:
                    counters.unknown_usage_calls += 1
                call_cost = self._pricing.cost_usd(call_usage)
                counters.input_tokens += call_usage.input_tokens
                counters.output_tokens += call_usage.output_tokens
                counters.cached_input_tokens += call_usage.cached_input_tokens
                counters.cost_usd += call_cost
                await self._append(
                    log,
                    actual_run_id,
                    step_id,
                    Message(
                        Role.ASSISTANT,
                        response.content,
                        tool_calls=response.tool_calls,
                        # Kept so it can be echoed back on the next request: DeepSeek
                        # rejects a tool-carrying request with a 400 if the reasoning
                        # from earlier turns is missing.
                        reasoning_content=response.reasoning_content or None,
                    ),
                )
                await self._emit(
                    "model_call_completed",
                    actual_run_id,
                    step_id,
                    {
                        "finish_reason": response.finish_reason,
                        "content": response.content,
                        "tool_calls": [
                            {
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in response.tool_calls
                        ],
                        "tool_call_count": len(response.tool_calls),
                        "input_tokens": response.usage.input_tokens,
                        "usage_known": response.usage_known,
                        "provider_model": response.provider_model,
                        "system_fingerprint": response.system_fingerprint,
                        "output_tokens": response.usage.output_tokens,
                        "format_retries": response.format_retries,
                        "cached_input_tokens": response.usage.cached_input_tokens,
                        "cost_usd": call_cost,
                        "duration_ms": model_duration_ms,
                        "inference": (
                            {
                                "ttft_ms": response.inference.ttft_ms,
                                "prefill_tokens_per_second": (
                                    response.inference.prefill_tokens_per_second
                                ),
                                "decode_tokens_per_second": (
                                    response.inference.decode_tokens_per_second
                                ),
                                "peak_memory_gb": response.inference.peak_memory_gb,
                            }
                            if response.inference is not None
                            else None
                        ),
                    },
                )

                if (
                    counters.token_usage.total_tokens >= self._budget.max_tokens
                    or counters.unknown_usage_calls > 0
                    or time.monotonic() - started >= self._budget.max_duration_seconds
                ):
                    continue

                if response.recoverable_error is not None:
                    counters.format_failures += 1
                    error = response.recoverable_error
                    await self._emit(
                        "model_output_rejected",
                        actual_run_id,
                        step_id,
                        {"code": error.code, "message": error.message},
                    )
                    if counters.format_failures >= _MAX_FORMAT_FAILURES:
                        return await self._finish(
                            actual_run_id,
                            RunStatus.FAILED,
                            log,
                            counters,
                            started,
                            failure_reason=f"{error.code}: {error.message}",
                        )
                    await self._append(
                        log,
                        actual_run_id,
                        step_id,
                        Message(
                            Role.SYSTEM,
                            f"Your previous action was rejected ({error.code}). "
                            "Choose one available tool and use its exact input schema, "
                            "or finish with an answer. Return one JSON action.",
                            name="model_feedback",
                        ),
                    )
                    continue
                counters.format_failures = 0

                if not response.tool_calls:
                    if await self._unresolved_finish(plan, counters, log, actual_run_id, step_id):
                        if counters.rejected_finishes >= _MAX_REJECTED_FINISHES:
                            return await self._finish(
                                actual_run_id,
                                RunStatus.FAILED,
                                log,
                                counters,
                                started,
                                failure_reason=_UNRESOLVED_EDIT_REASON,
                            )
                        continue
                    if self._extensions is not None:

                        async def invoke(
                            name: str, arguments: dict[str, JsonValue]
                        ) -> ToolObservation:
                            if counters.policy_failure is not None:
                                raise RuntimeError(counters.policy_failure)
                            observation = await self._extension_call(
                                name, arguments, actual_run_id, counters, started, log
                            )
                            reason = await self._observe_action(
                                ToolCall(observation.call_id, name, arguments),
                                observation,
                                plan,
                                guard,
                                counters,
                                log,
                                actual_run_id,
                                counters.steps,
                            )
                            if reason is not None:
                                counters.policy_failure = reason
                                raise RuntimeError(reason)
                            if plan is not None:
                                await self._append(
                                    log,
                                    actual_run_id,
                                    counters.steps,
                                    Message(Role.SYSTEM, plan.render(), name="plan_state"),
                                )
                            return observation

                        caller = _RunCaller(
                            invoke, lambda: self._hook_view(actual_run_id, counters, started, log)
                        )
                        try:
                            proposal = FinishProposal(caller.snapshot(), response.content, caller)
                            finish_decision = await self._extensions.decide(proposal)
                        finally:
                            caller.close()
                        await self._emit(
                            "finish_decided",
                            actual_run_id,
                            step_id,
                            {"action": finish_decision.action.value},
                        )
                        if self._budget_reason(counters, started, include_step=False) is not None:
                            continue
                        if counters.policy_failure is not None:
                            return await self._finish(
                                actual_run_id,
                                RunStatus.FAILED,
                                log,
                                counters,
                                started,
                                failure_reason=counters.policy_failure,
                            )
                        if await self._unresolved_finish(
                            plan, counters, log, actual_run_id, step_id
                        ):
                            if counters.rejected_finishes >= _MAX_REJECTED_FINISHES:
                                return await self._finish(
                                    actual_run_id,
                                    RunStatus.FAILED,
                                    log,
                                    counters,
                                    started,
                                    failure_reason=_UNRESOLVED_EDIT_REASON,
                                )
                            continue
                        if finish_decision.action is FinishAction.CONTINUE:
                            await self._append(
                                log,
                                actual_run_id,
                                step_id,
                                Message(
                                    Role.USER,
                                    redact_text(finish_decision.feedback),
                                    name="extension_feedback",
                                ),
                            )
                            continue
                        if finish_decision.action is FinishAction.FAIL:
                            return await self._finish(
                                actual_run_id,
                                RunStatus.FAILED,
                                log,
                                counters,
                                started,
                                failure_reason=redact_text(finish_decision.feedback),
                            )
                    return await self._finish(
                        actual_run_id,
                        RunStatus.FINISHED,
                        log,
                        counters,
                        started,
                        answer=response.content,
                    )

                for call in response.tool_calls:
                    if (
                        counters.tool_calls >= self._budget.max_tool_calls
                        or time.monotonic() - started >= self._budget.max_duration_seconds
                    ):
                        break
                    counters.tool_calls += 1
                    async with asyncio.timeout(
                        max(0.0, self._budget.max_duration_seconds - (time.monotonic() - started))
                    ):
                        with (
                            self._extensions.tool_scope(
                                self._hook_view(actual_run_id, counters, started, log)
                            )
                            if self._extensions
                            else nullcontext()
                        ):
                            observation = await self._tools.execute(call, actual_run_id, step_id)
                    counters.tool_ms += observation.duration_ms
                    await self._append(
                        log, actual_run_id, step_id, self._observation_message(observation)
                    )
                    policy_failure = await self._observe_action(
                        call, observation, plan, guard, counters, log, actual_run_id, step_id
                    )
                    if policy_failure is not None:
                        return await self._finish(
                            actual_run_id,
                            RunStatus.FAILED,
                            log,
                            counters,
                            started,
                            failure_reason=policy_failure,
                        )
                if plan is not None:
                    await self._append(
                        log,
                        actual_run_id,
                        step_id,
                        Message(Role.SYSTEM, plan.render(), name="plan_state"),
                    )
                    await self._emit(
                        "plan_state_updated",
                        actual_run_id,
                        step_id,
                        {"state": json.loads(plan.render())},
                    )
                if self._reflection_enabled:
                    await self._append(
                        log,
                        actual_run_id,
                        step_id,
                        Message(
                            Role.SYSTEM,
                            "Reflection checkpoint: review the latest tool observations, "
                            "update the plan, and avoid repeating failed actions.",
                            name="reflection",
                        ),
                    )
                    await self._emit("reflection_prompt_added", actual_run_id, step_id)

        except asyncio.CancelledError:
            await self._finish(
                actual_run_id,
                RunStatus.CANCELLED,
                log,
                counters,
                started,
                failure_reason="run cancelled",
            )
            raise
        except Exception as exc:
            return await self._finish(
                actual_run_id,
                RunStatus.BUDGET_EXHAUSTED
                if isinstance(exc, ExtensionBudgetExhaustedError)
                or time.monotonic() - started >= self._budget.max_duration_seconds
                else RunStatus.FAILED,
                log,
                counters,
                started,
                failure_reason=counters.policy_failure or _failure_reason(exc),
            )

    async def _unresolved_finish(
        self,
        plan: LocalPlanState | None,
        counters: _RunCounters,
        log: SessionLog,
        run_id: str,
        step_id: int,
    ) -> bool:
        if plan is None or not plan.unresolved_edit:
            return False
        counters.rejected_finishes += 1
        await self._emit(
            "premature_finish_rejected",
            run_id,
            step_id,
            {"reason": "unresolved_edit"},
        )
        if counters.rejected_finishes < _MAX_REJECTED_FINISHES:
            await self._append(
                log,
                run_id,
                step_id,
                Message(
                    Role.SYSTEM,
                    "The last workspace edit failed; the file is not fixed. "
                    "Use the exact text in the latest read_file result as "
                    "patch_file.expected, then supply the corrected replacement "
                    "and run the test. Do not finish before the edit succeeds.",
                    name="model_feedback",
                ),
            )
        return True

    async def _observe_action(  # noqa: PLR0913, PLR0917 - shared policy for all action sources
        self,
        call: ToolCall,
        observation: ToolObservation,
        plan: LocalPlanState | None,
        guard: RepetitionGuard | None,
        counters: _RunCounters,
        log: SessionLog,
        run_id: str,
        step_id: int,
    ) -> str | None:
        if plan is not None and guard is not None:
            call = observation.effective_call or call
            plan.observe(call, observation)
            if not plan.unresolved_edit:
                counters.rejected_finishes = 0
            decision = guard.observe(call, observation)
            if decision == "recover":
                await self._emit(
                    "loop_guard_triggered",
                    run_id,
                    step_id,
                    {
                        "count": max(guard.count, guard.error_count),
                        "tool": call.name,
                        "error": guard.error_signature,
                    },
                )
                await self._append(
                    log,
                    run_id,
                    step_id,
                    Message(
                        Role.SYSTEM,
                        "The same action or error has repeated three times. "
                        "Change strategy; inspect the exact failing lines or "
                        "explain the blocker.",
                        name="recovery_hint",
                    ),
                )
            if decision == "stop":
                return "loop_stuck: six repeated actions or equivalent errors"
        return None

    async def _extension_call(  # noqa: PLR0913, PLR0917 - explicit run-scoped accounting
        self,
        name: str,
        arguments: dict[str, JsonValue],
        run_id: str,
        counters: _RunCounters,
        started: float,
        log: SessionLog,
    ) -> ToolObservation:
        reason = self._budget_reason(counters, started, include_step=False)
        if reason is not None:
            raise ExtensionBudgetExhaustedError(reason)
        call = ToolCall("extension-" + uuid.uuid4().hex, name, deepcopy(arguments))
        counters.tool_calls += 1
        await self._emit(
            "extension_tool_requested",
            run_id,
            counters.steps,
            {"call_id": call.call_id, "tool": name},
        )
        await self._append(
            log, run_id, counters.steps, Message(Role.ASSISTANT, "", tool_calls=(call,))
        )
        action_started = time.monotonic()
        try:
            async with asyncio.timeout(
                max(0.0, self._budget.max_duration_seconds - (time.monotonic() - started))
            ):
                with (
                    self._extensions.tool_scope(self._hook_view(run_id, counters, started, log))
                    if self._extensions
                    else nullcontext()
                ):
                    observation = await self._tools.execute(call, run_id, counters.steps)
        finally:
            counters.tool_ms += (time.monotonic() - action_started) * 1000
        await self._append(log, run_id, counters.steps, self._observation_message(observation))
        return observation

    def _hook_view(
        self, run_id: str, counters: _RunCounters, started: float, log: SessionLog
    ) -> HookView:
        return HookView(
            run_id,
            counters.steps,
            BudgetView(
                max(0, self._budget.max_steps - counters.steps),
                max(0, self._budget.max_tokens - counters.token_usage.total_tokens),
                max(0, self._budget.max_tool_calls - counters.tool_calls),
                max(0.0, self._budget.max_duration_seconds - (time.monotonic() - started)),
            ),
            log.messages(),
        )

    async def _end_step(
        self, run_id: str, counters: _RunCounters, started: float, log: SessionLog
    ) -> None:
        if counters.steps > counters.closed_step:
            counters.closed_step = counters.steps
            if self._extensions is not None:
                await self._extensions.step_ended(self._hook_view(run_id, counters, started, log))

    def _continue_history(self, prior: tuple[Message, ...]) -> tuple[list[Message], int]:
        """Close interrupted tool rounds in the new view; never rewrite the old run."""
        history = [Message(Role.SYSTEM, self._system_prompt)]
        pending: dict[str, str] = {}
        repaired = 0

        def close_pending() -> None:
            nonlocal repaired
            for call_id, name in pending.items():
                history.append(
                    Message(
                        Role.TOOL,
                        json.dumps(
                            {
                                "status": "failed",
                                "error": {
                                    "code": "interrupted",
                                    "message": "The prior run ended before an observation "
                                    "was recorded. "
                                    "Execution outcome is unknown; inspect state before retrying.",
                                },
                            }
                        ),
                        name=name,
                        tool_call_id=call_id,
                    )
                )
                repaired += 1
            pending.clear()

        for index, message in enumerate(prior):
            if index == 0 and message.role is Role.SYSTEM:
                continue
            if message.role is Role.TOOL:
                if message.tool_call_id not in pending:
                    raise ValueError("prior history contains an orphan tool observation")
                pending.pop(message.tool_call_id)
            else:
                close_pending()
            history.append(message)
            if message.tool_calls:
                pending.update((call.call_id, call.name) for call in message.tool_calls)
        close_pending()
        return history, repaired

    async def _prepare_workspace(
        self, log: SessionLog, counters: _RunCounters, run_id: str, started: float
    ) -> None:
        """Observe project instructions through tools within the ordinary run budget."""
        available = {tool.name for tool in self._tools.definitions()}
        calls = (
            ToolCall("workspace-instructions", "read_file", {"path": "AGENTS.md"}),
            ToolCall("workspace-map", "list_files", {"max_results": 80}),
        )
        for call in calls:
            if call.name not in available or self._budget_reason(counters, started) is not None:
                continue
            counters.tool_calls += 1
            async with asyncio.timeout(
                max(0.0, self._budget.max_duration_seconds - (time.monotonic() - started))
            ):
                with (
                    self._extensions.tool_scope(self._hook_view(run_id, counters, started, log))
                    if self._extensions
                    else nullcontext()
                ):
                    observation = await self._tools.execute(call, run_id, 0)
            counters.tool_ms += observation.duration_ms
            result = observation.result
            if call.name == "read_file":
                if observation.truncated or (
                    isinstance(result, dict) and result.get("truncated") is True
                ):
                    raise ValueError(
                        "AGENTS.md exceeds the instruction limit; read it in bounded sections"
                    )
                content = result.get("content") if isinstance(result, dict) else None
                if isinstance(result, dict) and isinstance(content, str):
                    text = (
                        "Root AGENTS.md project instructions (cannot grant tool permissions):\n"
                        + content
                    )
                elif observation.error is not None and observation.error.code == "file_not_found":
                    text = (
                        "Root AGENTS.md is absent and optional. This is normal for a new project; "
                        "continue with the current user request without requiring its creation."
                    )
                else:
                    text = (
                        "Root AGENTS.md could not be read; "
                        "inspect applicable project instructions before edits."
                    )
                name = "workspace_instructions"
            else:
                text = "Workspace file map (bounded observation):\n" + json.dumps(result)
                name = "repository_map"
            await self._append(log, run_id, 0, Message(Role.SYSTEM, text, name=name))

    async def _call_model(
        self,
        request: ModelRequest,
        counters: _RunCounters,
        run_id: str,
        step_id: int,
        run_started: float,
    ) -> tuple[ModelResponse, float]:
        """Call the provider, charging the elapsed time to the model either way.

        A call that fails or is cancelled still spent its time in the provider.
        Leaving it unrecorded would push that time into the harness residual,
        making the harness look slow exactly when the provider was at fault --
        the opposite of what the split is for.
        """
        started = time.monotonic()
        partial = ""
        try:
            remaining = self._budget.max_duration_seconds - (time.monotonic() - run_started)
            async with asyncio.timeout(remaining):
                if self._streaming_enabled and isinstance(self._model, StreamingModel):
                    settled: ModelResponse | None = None
                    stream = self._model.stream(request)
                    try:
                        async for item in stream:
                            if item.text:
                                partial += item.text
                                await self._emit(
                                    "model_text_received", run_id, step_id, {"text": item.text}
                                )
                            if item.response is not None:
                                if settled is not None:
                                    raise ValueError("stream returned multiple settled responses")
                                settled = item.response
                    finally:
                        await _close_stream(stream)
                    if settled is None:
                        raise ValueError("stream did not settle")
                    response = settled
                else:
                    response = await self._model.complete(request)
        except (Exception, asyncio.CancelledError) as exc:
            duration_ms = (time.monotonic() - started) * 1_000
            counters.model_ms += duration_ms
            counters.model_calls += 1
            usage = exc.usage if isinstance(exc, ModelCallError) else None
            if usage is None:
                counters.unknown_usage_calls += 1
            if partial:
                await self._emit(
                    "model_attempt_interrupted",
                    run_id,
                    step_id,
                    {"content": partial, "usage_known": usage is not None},
                )
            cost = self._pricing.cost_usd(usage) if usage is not None else None
            if usage is not None:
                counters.input_tokens += usage.input_tokens
                counters.output_tokens += usage.output_tokens
                counters.cached_input_tokens += usage.cached_input_tokens
                counters.cost_usd += cost or 0.0
            await self._emit(
                "model_call_failed",
                run_id,
                step_id,
                {
                    "error": _failure_reason(exc),
                    "usage_known": usage is not None,
                    "input_tokens": usage.input_tokens if usage is not None else None,
                    "output_tokens": usage.output_tokens if usage is not None else None,
                    "cached_input_tokens": usage.cached_input_tokens if usage is not None else None,
                    "cost_usd": cost,
                    "duration_ms": duration_ms,
                },
            )
            raise
        duration_ms = (time.monotonic() - started) * 1_000
        counters.model_ms += duration_ms
        counters.model_calls += 1
        return response, duration_ms

    def _budget_reason(
        self, counters: _RunCounters, started: float, *, include_step: bool = True
    ) -> str | None:
        if counters.unknown_usage_calls:
            return "model token usage is unknown; no further calls permitted"
        if include_step and counters.steps >= self._budget.max_steps:
            return "step budget exhausted"
        if counters.token_usage.total_tokens >= self._budget.max_tokens:
            return "token budget exhausted"
        if counters.tool_calls >= self._budget.max_tool_calls:
            return "tool-call budget exhausted"
        if time.monotonic() - started >= self._budget.max_duration_seconds:
            return "duration budget exhausted"
        return None

    def _observation_message(self, observation: ToolObservation) -> Message:
        payload: dict[str, object] = {
            "status": observation.status.value,
            "result": observation.result,
        }
        if observation.summary:
            payload["summary"] = observation.summary
        if self._detailed_tool_feedback:
            # ``duration_ms`` is deliberately excluded: it is wall-clock telemetry that
            # belongs to the event stream, and its variable-precision float would make
            # the rendered context (and therefore token counts) differ between runs.
            payload.update(
                {
                    "truncated": observation.truncated,
                    "retryable": observation.retryable,
                    "artifacts": list(observation.artifacts),
                }
            )
        if observation.error is not None:
            payload["error"] = {
                "code": observation.error.code,
                "message": observation.error.message,
                "details": observation.error.details,
            }
        return Message(
            Role.TOOL,
            json.dumps(payload, sort_keys=True),
            name=observation.tool_name,
            tool_call_id=observation.call_id,
        )

    @staticmethod
    def _message_json(message: Message) -> dict[str, object]:
        return {
            "role": message.role.value,
            "content": message.content,
            "name": message.name,
            "tool_call_id": message.tool_call_id,
            "reasoning_content": message.reasoning_content,
            "tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ],
        }

    async def _finish(  # noqa: PLR0913 - terminal state fields stay explicit
        self,
        run_id: str,
        status: RunStatus,
        log: SessionLog,
        counters: _RunCounters,
        started: float,
        *,
        answer: str = "",
        failure_reason: str | None = None,
    ) -> RunResult:
        await self._end_step(run_id, counters, started, log)
        exhausted = self._budget_reason(counters, started, include_step=False)
        if status is RunStatus.FINISHED and exhausted is not None:
            status, failure_reason, answer = RunStatus.BUDGET_EXHAUSTED, exhausted, ""
        duration_ms = (time.monotonic() - started) * 1_000
        latency = counters.latency(duration_ms)
        result = RunResult(
            run_id=run_id,
            status=status,
            answer=answer,
            steps=counters.steps,
            token_usage=counters.token_usage,
            tool_calls=counters.tool_calls,
            duration_ms=duration_ms,
            failure_reason=failure_reason,
            history=log.messages(),
            latency=latency,
            unknown_usage_calls=counters.unknown_usage_calls,
        )
        if counters.cleanup_deadline is None:
            counters.cleanup_deadline = time.monotonic() + 1.0
        try:
            if self._hooks is not None and not counters.legacy_finished:
                counters.legacy_finished = True
                async with asyncio.timeout(max(0.0, counters.cleanup_deadline - time.monotonic())):
                    await self._hooks.finishing(result)
        except Exception:
            await self._emit("hook_failed", run_id, counters.steps, {"phase": "before_finish"})
            result = replace(result, status=RunStatus.FAILED, failure_reason="finish_hook_failed")
        await self._emit(
            "run_finished",
            run_id,
            counters.steps,
            {
                "status": result.status.value,
                "steps": counters.steps,
                "tokens": counters.token_usage.total_tokens,
                "cached_input_tokens": counters.cached_input_tokens,
                "cost_usd": counters.cost_usd,
                "tool_calls": counters.tool_calls,
                "unknown_usage_calls": counters.unknown_usage_calls,
                "duration_ms": duration_ms,
                "latency": latency.to_json(),
                "failure_reason": result.failure_reason,
            },
        )
        release = getattr(self._tools, "release_run", None)
        if callable(release):
            release(run_id)
        if self._sessions is not None:
            try:
                await self._sessions.save(result)
            except Exception:
                await self._emit("session_save_failed", run_id, counters.steps)
        if self._extensions is not None:
            await self._extensions.ended(
                result, timeout_seconds=max(0.0, counters.cleanup_deadline - time.monotonic())
            )
        return result

    async def _append(self, log: SessionLog, run_id: str, step_id: int, message: Message) -> None:
        event = await log.append(step_id, message)
        await self._emit(
            "session_message_committed",
            run_id,
            step_id,
            {
                "sequence": event.sequence,
                "event_name": event.name,
                "message": self._message_json(event.message),
            },
        )

    async def _emit(
        self,
        name: str,
        run_id: str,
        step_id: int,
        data: dict[str, object] | None = None,
    ) -> None:
        safe_data = data or {}
        await self._events.publish(
            Event(
                name=name,
                run_id=run_id,
                step_id=step_id,
                data=safe_data,  # type: ignore[arg-type]
            )
        )


class AgentLoopFactory:
    """Default driver factory, replaceable through the plugin service contract."""

    def create(self, dependencies: AgentDependencies) -> AgentRunner:
        """Build the loop from supplied ports without looking up global services."""
        return AgentLoop(
            model=dependencies.model,
            context_manager=dependencies.context_manager,
            tools=dependencies.tools,
            events=dependencies.events,
            budget=dependencies.budget,
            pricing=dependencies.pricing,
            system_prompt=dependencies.system_prompt,
            reflection_enabled=dependencies.reflection_enabled,
            detailed_tool_feedback=dependencies.detailed_tool_feedback,
            sessions=dependencies.sessions,
            journal=dependencies.journal,
            local_first_enabled=dependencies.local_first_enabled,
            error_recovery_enabled=dependencies.error_recovery_enabled,
            workspace_instructions_enabled=dependencies.workspace_instructions_enabled,
            streaming_enabled=dependencies.streaming_enabled,
            hooks=dependencies.hooks,
            request_input_budget=dependencies.request_input_budget,
        )

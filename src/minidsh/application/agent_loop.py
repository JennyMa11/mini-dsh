"""The provider-agnostic Observe/Plan/Act/Execute agent loop."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass

from minidsh.domain import (
    AgentBudget,
    Event,
    LatencyBreakdown,
    Message,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    Role,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolObservation,
)
from minidsh.ports import ContextManager, EventSink, Model, SessionStore, ToolExecutor


@dataclass(slots=True)
class _RunCounters:
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    model_calls: int = 0

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

    async def run(self, task: str, *, run_id: str | None = None) -> RunResult:
        """Run until the model finishes, a failure occurs, or a budget is exhausted."""
        actual_run_id = run_id or uuid.uuid4().hex
        started = time.monotonic()
        counters = _RunCounters()
        history: list[Message] = [
            Message(Role.SYSTEM, self._system_prompt),
            Message(Role.USER, task),
        ]
        await self._emit("run_started", actual_run_id, 0, {"task_length": len(task)})

        try:
            while True:
                exhausted = self._budget_reason(counters, started)
                if exhausted is not None:
                    return await self._finish(
                        actual_run_id,
                        RunStatus.BUDGET_EXHAUSTED,
                        history,
                        counters,
                        started,
                        failure_reason=exhausted,
                    )

                counters.steps += 1
                step_id = counters.steps
                context = self._context.build(tuple(history), task)
                await self._emit(
                    "context_built",
                    actual_run_id,
                    step_id,
                    {
                        "strategy": context.strategy,
                        "version": context.version,
                        "estimated_tokens": context.estimated_tokens,
                        "omitted_messages": context.omitted_messages,
                    },
                )
                request = ModelRequest(context.messages, self._tools.definitions())
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
                response, model_duration_ms = await self._call_model(request, counters)
                call_usage = response.usage
                call_cost = self._pricing.cost_usd(call_usage)
                counters.input_tokens += call_usage.input_tokens
                counters.output_tokens += call_usage.output_tokens
                counters.cached_input_tokens += call_usage.cached_input_tokens
                counters.cost_usd += call_cost
                history.append(
                    Message(
                        Role.ASSISTANT,
                        response.content,
                        tool_calls=response.tool_calls,
                        # Kept so it can be echoed back on the next request: DeepSeek
                        # rejects a tool-carrying request with a 400 if the reasoning
                        # from earlier turns is missing.
                        reasoning_content=response.reasoning_content or None,
                    )
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
                        "output_tokens": response.usage.output_tokens,
                        "cached_input_tokens": response.usage.cached_input_tokens,
                        "cost_usd": call_cost,
                        "duration_ms": model_duration_ms,
                    },
                )

                if counters.token_usage.total_tokens >= self._budget.max_tokens:
                    continue

                if not response.tool_calls:
                    return await self._finish(
                        actual_run_id,
                        RunStatus.FINISHED,
                        history,
                        counters,
                        started,
                        answer=response.content,
                    )

                for call in response.tool_calls:
                    if counters.tool_calls >= self._budget.max_tool_calls:
                        break
                    counters.tool_calls += 1
                    observation = await self._tools.execute(call, actual_run_id, step_id)
                    counters.tool_ms += observation.duration_ms
                    history.append(self._observation_message(observation))
                if self._reflection_enabled:
                    history.append(
                        Message(
                            Role.SYSTEM,
                            "Reflection checkpoint: review the latest tool observations, "
                            "update the plan, and avoid repeating failed actions.",
                            name="reflection",
                        )
                    )
                    await self._emit("reflection_prompt_added", actual_run_id, step_id)

        except asyncio.CancelledError:
            await self._finish(
                actual_run_id,
                RunStatus.CANCELLED,
                history,
                counters,
                started,
                failure_reason="run cancelled",
            )
            raise
        except Exception as exc:
            return await self._finish(
                actual_run_id,
                RunStatus.FAILED,
                history,
                counters,
                started,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )

    async def _call_model(
        self,
        request: ModelRequest,
        counters: _RunCounters,
    ) -> tuple[ModelResponse, float]:
        """Call the provider, charging the elapsed time to the model either way.

        A call that fails or is cancelled still spent its time in the provider.
        Leaving it unrecorded would push that time into the harness residual,
        making the harness look slow exactly when the provider was at fault --
        the opposite of what the split is for.
        """
        started = time.monotonic()
        try:
            response = await self._model.complete(request)
        except (Exception, asyncio.CancelledError):
            counters.model_ms += (time.monotonic() - started) * 1_000
            counters.model_calls += 1
            raise
        duration_ms = (time.monotonic() - started) * 1_000
        counters.model_ms += duration_ms
        counters.model_calls += 1
        return response, duration_ms

    def _budget_reason(self, counters: _RunCounters, started: float) -> str | None:
        if counters.steps >= self._budget.max_steps:
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
            "tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ],
        }

    async def _finish(  # noqa: PLR0913 - terminal state fields stay explicit
        self,
        run_id: str,
        status: RunStatus,
        history: list[Message],
        counters: _RunCounters,
        started: float,
        *,
        answer: str = "",
        failure_reason: str | None = None,
    ) -> RunResult:
        duration_ms = (time.monotonic() - started) * 1_000
        latency = counters.latency(duration_ms)
        await self._emit(
            "run_finished",
            run_id,
            counters.steps,
            {
                "status": status.value,
                "steps": counters.steps,
                "tokens": counters.token_usage.total_tokens,
                "cached_input_tokens": counters.cached_input_tokens,
                "cost_usd": counters.cost_usd,
                "tool_calls": counters.tool_calls,
                "duration_ms": duration_ms,
                "latency": latency.to_json(),
                "failure_reason": failure_reason,
            },
        )
        result = RunResult(
            run_id=run_id,
            status=status,
            answer=answer,
            steps=counters.steps,
            token_usage=counters.token_usage,
            tool_calls=counters.tool_calls,
            duration_ms=duration_ms,
            failure_reason=failure_reason,
            history=tuple(history),
            latency=latency,
        )
        if self._sessions is not None:
            try:
                await self._sessions.save(result)
            except Exception:
                await self._emit("session_save_failed", run_id, counters.steps)
        return result

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

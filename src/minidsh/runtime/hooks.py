"""Ordered trusted hooks, separate from failure-isolating observation events."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial

from minidsh.domain import (
    Event,
    Message,
    ModelRequest,
    Role,
    RunResult,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    redact_text,
)
from minidsh.domain.extensions import (
    ExtensionBudgetExhaustedError,
    FinishAction,
    FinishDecision,
    FinishProposal,
    HookView,
)
from minidsh.ports.protocols import EventSink

type RequestHook = Callable[[ModelRequest], Awaitable[ModelRequest]]
type ToolHook = Callable[[ToolCall], Awaitable[ToolCall]]
type ObservationHook = Callable[[ToolObservation], Awaitable[None]]
type FinishHook = Callable[[RunResult], Awaitable[None]]


class HookRejectedError(ValueError):
    """A policy hook explicitly rejected a request or call."""


class _ContractCode(StrEnum):
    CAPABILITIES = "model hooks cannot change tool capabilities"
    PROTECTED = "model hooks cannot remove protected messages"
    IDENTITY = "tool hooks cannot change call identity"
    DECISION = "invalid finish decision"


class _ContractError(HookRejectedError):
    def __init__(self, code: _ContractCode) -> None:
        if not isinstance(code, _ContractCode):
            raise ValueError("invalid contract code")
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class _Hooks:
    owner: str
    priority: int
    sequence: int
    before_request: RequestHook | None
    before_tool: ToolHook | None
    after_tool: ObservationHook | None
    before_finish: FinishHook | None


class HookRegistry:
    def __init__(self, events: EventSink | None = None) -> None:
        self._events = events
        self._active_view: ContextVar[HookView | None] = ContextVar("hook_view", default=None)
        self._modern: list[_ModernHooks] = []
        self._entries: list[_Hooks] = []
        self._sequence = 0

    def register(  # noqa: PLR0913 - explicit independent extension points
        self,
        owner: str,
        *,
        priority: int = 0,
        before_request: RequestHook | None = None,
        before_tool: ToolHook | None = None,
        after_tool: ObservationHook | None = None,
        before_finish: FinishHook | None = None,
    ) -> Callable[[], None]:
        """Register explicitly and return an idempotent lifecycle cleanup."""
        entry = _Hooks(
            owner, priority, self._sequence, before_request, before_tool, after_tool, before_finish
        )
        self._sequence += 1
        self._entries.append(entry)

        def remove() -> None:
            if entry in self._entries:
                self._entries.remove(entry)

        return remove

    def _ordered(self) -> tuple[_Hooks, ...]:
        return tuple(
            sorted(self._entries, key=lambda item: (item.priority, item.owner, item.sequence))
        )

    async def request(self, request: ModelRequest) -> ModelRequest:
        original_tools = deepcopy(request.tools)
        protected = deepcopy(
            tuple(m for m in request.messages if m.role in {Role.SYSTEM, Role.USER})
        )
        request = deepcopy(request)
        for entry in self._ordered():
            if entry.before_request is not None:
                request = await self._legacy_invoke(
                    entry,
                    "before_request",
                    partial(
                        self._checked_legacy_request,
                        entry.before_request,
                        request,
                        original_tools,
                        protected,
                    ),
                )
        return request

    async def call(self, call: ToolCall) -> ToolCall:
        original_id = call.call_id
        call = deepcopy(call)
        for entry in self._ordered():
            if entry.before_tool is not None:
                call = await self._legacy_invoke(
                    entry,
                    "before_tool",
                    partial(self._checked_legacy_call, entry.before_tool, call, original_id),
                )
        return call

    async def observed(self, observation: ToolObservation) -> None:
        for entry in self._ordered():
            if entry.after_tool is not None:
                await self._legacy_invoke(
                    entry, "after_tool", partial(entry.after_tool, deepcopy(observation))
                )

    async def finishing(self, result: RunResult) -> None:
        for entry in self._ordered():
            if entry.before_finish is not None:
                await self._legacy_invoke(
                    entry,
                    "legacy_before_finish",
                    partial(entry.before_finish, deepcopy(result)),
                    state=HookView(result.run_id, result.steps, None),
                )

    @contextmanager
    def tool_scope(self, state: HookView) -> Iterator[None]:
        token = self._active_view.set(state)
        try:
            yield
        finally:
            self._active_view.reset(token)

    def tool_view(self, run_id: str, step_id: int) -> HookView:
        state = self._active_view.get()
        if state is not None and state.run_id == run_id:
            return replace(state, step_id=step_id)
        return HookView(run_id, step_id, None)

    def bind_events(self, events: EventSink) -> None:
        """Composition binds audit delivery before the first operation."""
        self._events = events

    def register_v2(  # noqa: PLR0913 - explicit optional phase contracts
        self,
        owner: str,
        version: str,
        *,
        on_run_start: StateHook | None = None,
        before_model: ModelHook | None = None,
        before_tool: ModernToolHook | None = None,
        after_tool: ModernObservationHook | None = None,
        on_step_end: StateHook | None = None,
        before_finish: DecisionHook | None = None,
        on_run_end: EndHook | None = None,
    ) -> Callable[[], None]:
        """V2 always follows registration order; legacy dispatch stays separate."""
        if (
            re.fullmatch(r"[a-z][a-z0-9_]{0,127}", owner) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version) is None
        ):
            raise ValueError("extension identity requires a bounded snake_case ID and version")
        entry = _ModernHooks(
            owner,
            version,
            self._sequence,
            on_run_start,
            before_model,
            before_tool,
            after_tool,
            on_step_end,
            before_finish,
            on_run_end,
        )
        self._sequence += 1
        self._modern.append(entry)

        def remove() -> None:
            if entry in self._modern:
                self._modern.remove(entry)

        return remove

    def order_manifest(self) -> tuple[tuple[str, str, int], ...]:
        """Explicit mixed plan: sorted v1 block, then registration-ordered v2 block."""
        return tuple((e.owner, "legacy_v1", e.sequence) for e in self._ordered()) + tuple(
            (e.owner, e.version, e.sequence) for e in self._modern
        )

    def is_final_finish_checker(self, callback: DecisionHook) -> bool:
        """Read the completion order without reordering or running any callback."""
        callbacks = [
            entry.before_finish for entry in self._modern if entry.before_finish is not None
        ]
        return bool(callbacks) and callbacks[-1] == callback

    @staticmethod
    async def _checked_legacy_request(
        callback: RequestHook,
        request: ModelRequest,
        tools: tuple[ToolDefinition, ...],
        protected: tuple[Message, ...],
    ) -> ModelRequest:
        result = await callback(request)
        if result.tools != tools:
            raise HookRejectedError("request hooks cannot change the tool capability set")
        kept = iter(result.messages)
        if not all(any(m == required for m in kept) for required in protected):
            raise HookRejectedError("request hooks cannot remove protected messages")
        return result

    @staticmethod
    async def _checked_legacy_call(callback: ToolHook, call: ToolCall, identity: str) -> ToolCall:
        result = await callback(call)
        if result.call_id != identity:
            raise HookRejectedError("tool hooks cannot change call identity")
        return result

    async def _legacy_invoke[T](
        self,
        entry: _Hooks,
        phase: str,
        operation: Callable[[], Awaitable[T]],
        *,
        state: HookView | None = None,
    ) -> T:
        """Audit the compatibility path while preserving original exception semantics."""
        state = state or self._active_view.get()
        started = time.monotonic()
        outcome, error = "completed", ""
        token = _TOOL_HOOK_ACTIVE.set(
            _TOOL_HOOK_ACTIVE.get()
            or phase in {"before_tool", "after_tool", "legacy_before_finish"}
        )
        try:
            timeout = (
                state.remaining.duration_seconds
                if state is not None and state.remaining is not None
                else 30.0
            )
            async with asyncio.timeout(max(0.0, timeout)):
                return await operation()
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            error = type(exc).__name__[:128]
            # Direct callers retain the original exception; the Loop boundary sanitizes it.
            exc.__dict__["_extension_phase"] = phase
            raise
        finally:
            _TOOL_HOOK_ACTIVE.reset(token)
            if state is not None and self._events is not None:
                await self._events.publish(
                    Event(
                        "extension_hook_completed",
                        state.run_id,
                        state.step_id,
                        {
                            "extension_id": redact_text(entry.owner)[:128],
                            "extension_version": "legacy_v1",
                            "phase": phase,
                            "order": entry.sequence,
                            "duration_ms": (time.monotonic() - started) * 1000,
                            "outcome": outcome,
                            "error_type": error,
                            "transformation": "legacy_detached_view",
                        },
                    )
                )

    async def _invoke[T](
        self,
        entry: _ModernHooks,
        phase: str,
        state: HookView,
        operation: Callable[[], Awaitable[T]],
        deadline: float,
    ) -> T:
        started = time.monotonic()
        outcome = "completed"
        error = ""
        summary = "observed_detached_facts"
        token = _TOOL_HOOK_ACTIVE.set(
            _TOOL_HOOK_ACTIVE.get() or phase in {"before_tool", "after_tool", "on_run_end"}
        )
        try:
            async with asyncio.timeout(max(0.0, deadline - started)):
                result = await operation()
                if isinstance(result, ModelRequest):
                    summary = (
                        f"model_view:{len(result.messages)}_messages:{len(result.tools)}_tools"
                    )
                elif isinstance(result, ToolCall):
                    summary = "tool_call_revalidated"
                elif isinstance(result, FinishDecision):
                    summary = "finish_" + result.action.value
                return result
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            error = type(exc).__name__
            if isinstance(exc, (ExtensionBudgetExhaustedError, _ContractError)):
                raise
            if isinstance(exc, Exception):
                raise HookRejectedError(f"{phase} failed ({error})") from exc
            raise
        finally:
            _TOOL_HOOK_ACTIVE.reset(token)
            if self._events is not None:
                await self._events.publish(
                    Event(
                        "extension_hook_completed",
                        state.run_id,
                        state.step_id,
                        {
                            "extension_id": entry.owner,
                            "extension_version": entry.version,
                            "phase": phase,
                            "order": entry.sequence,
                            "duration_ms": (time.monotonic() - started) * 1000,
                            "outcome": outcome,
                            "error_type": error[:128],
                            "transformation": summary,
                        },
                    )
                )

    @staticmethod
    def _deadline(state: HookView) -> float:
        seconds = state.remaining.duration_seconds if state.remaining is not None else 30.0
        return time.monotonic() + max(0.0, seconds)

    @staticmethod
    async def _checked_model(
        callback: ModelHook, request: ModelRequest, state: HookView, original: ModelRequest
    ) -> ModelRequest:
        result = await callback(request, state)
        if not isinstance(result, ModelRequest) or result.tools != original.tools:
            raise _ContractError(_ContractCode.CAPABILITIES)
        protected = (m for m in original.messages if m.role in {Role.SYSTEM, Role.USER})
        kept = iter(result.messages)
        if not all(any(m == required for m in kept) for required in protected):
            raise _ContractError(_ContractCode.PROTECTED)
        return result

    @staticmethod
    async def _checked_call(
        callback: ModernToolHook, call: ToolCall, state: HookView, identity: str
    ) -> ToolCall:
        result = await callback(call, state)
        if not isinstance(result, ToolCall) or result.call_id != identity:
            raise _ContractError(_ContractCode.IDENTITY)
        return result

    @staticmethod
    async def _checked_decision(callback: DecisionHook, proposal: FinishProposal) -> FinishDecision:
        result = await callback(proposal)
        if not isinstance(result, FinishDecision):
            raise _ContractError(_ContractCode.DECISION)
        return result

    async def started(self, state: HookView) -> None:
        deadline = self._deadline(state)
        for entry in tuple(self._modern):
            callback = entry.on_run_start
            if callback is not None:
                await self._invoke(
                    entry, "on_run_start", state, partial(callback, deepcopy(state)), deadline
                )

    async def model(self, request: ModelRequest, state: HookView) -> ModelRequest:
        deadline = self._deadline(state)
        original = deepcopy(request)
        request = deepcopy(request)
        for entry in tuple(self._modern):
            callback = entry.before_model
            if callback is not None:
                request = await self._invoke(
                    entry,
                    "before_model",
                    state,
                    partial(
                        self._checked_model, callback, deepcopy(request), deepcopy(state), original
                    ),
                    deadline,
                )
        return request

    async def transform_call(self, call: ToolCall, state: HookView) -> ToolCall:
        deadline = self._deadline(state)
        identity = call.call_id
        call = deepcopy(call)
        for entry in tuple(self._modern):
            callback = entry.before_tool
            if callback is not None:
                call = await self._invoke(
                    entry,
                    "before_tool",
                    state,
                    partial(
                        self._checked_call, callback, deepcopy(call), deepcopy(state), identity
                    ),
                    deadline,
                )
        return call

    async def observe_all(self, observation: ToolObservation, state: HookView) -> None:
        deadline = self._deadline(state)
        for entry in tuple(self._modern):
            callback = entry.after_tool
            if callback is not None:
                with suppress(Exception):
                    await self._invoke(
                        entry,
                        "after_tool",
                        state,
                        partial(callback, deepcopy(observation), deepcopy(state)),
                        deadline,
                    )

    async def step_ended(self, state: HookView) -> None:
        deadline = self._deadline(state)
        for entry in tuple(self._modern):
            callback = entry.on_step_end
            if callback is not None:
                with suppress(Exception):
                    await self._invoke(
                        entry,
                        "on_step_end",
                        state,
                        partial(callback, deepcopy(state)),
                        deadline,
                    )

    async def decide(self, proposal: FinishProposal) -> FinishDecision:
        deadline = self._deadline(proposal.view)
        for entry in tuple(self._modern):
            callback = entry.before_finish
            if callback is not None:
                state = proposal.caller.snapshot() if proposal.caller is not None else proposal.view
                detached = replace(proposal, view=deepcopy(state))
                decision = await self._invoke(
                    entry,
                    "before_finish",
                    proposal.view,
                    partial(self._checked_decision, callback, detached),
                    deadline,
                )
                if decision.action is not FinishAction.ALLOW:
                    return decision
        return FinishDecision()

    async def ended(self, result: RunResult, *, timeout_seconds: float = 1.0) -> None:
        state = HookView(result.run_id, result.steps, None, ())
        deadline = time.monotonic() + min(1.0, max(0.0, timeout_seconds))
        for entry in tuple(self._modern):
            callback = entry.on_run_end
            if callback is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await self._invoke(
                        entry,
                        "on_run_end",
                        state,
                        partial(callback, deepcopy(result)),
                        deadline,
                    )


type StateHook = Callable[[HookView], Awaitable[None]]
type ModelHook = Callable[[ModelRequest, HookView], Awaitable[ModelRequest]]
type ModernToolHook = Callable[[ToolCall, HookView], Awaitable[ToolCall]]
type ModernObservationHook = Callable[[ToolObservation, HookView], Awaitable[None]]
type DecisionHook = Callable[[FinishProposal], Awaitable[FinishDecision]]
type EndHook = Callable[[RunResult], Awaitable[None]]

_TOOL_HOOK_ACTIVE: ContextVar[bool] = ContextVar("extension_tool_hook_active", default=False)


def tool_hook_active() -> bool:
    """Prevent a tool hook from entering the execution chain recursively."""
    return _TOOL_HOOK_ACTIVE.get()


@dataclass(frozen=True, slots=True)
class _ModernHooks:
    owner: str
    version: str
    sequence: int
    on_run_start: StateHook | None
    before_model: ModelHook | None
    before_tool: ModernToolHook | None
    after_tool: ModernObservationHook | None
    on_step_end: StateHook | None
    before_finish: DecisionHook | None
    on_run_end: EndHook | None

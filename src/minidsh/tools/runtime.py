"""Uniform schema/authorization/execution/observation tool pipeline."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from pydantic import BaseModel, ValidationError

from minidsh.domain import (
    ErrorInfo,
    Event,
    JsonValue,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
    redact_text,
)
from minidsh.domain.harness import ApprovalChoice, ApprovalRequest
from minidsh.ports import ApprovalProvider, EventSink
from minidsh.runtime.hooks import HookRegistry, tool_hook_active
from minidsh.tools.results import diagnostics, summarize


class ToolError(RuntimeError):
    """A safe, structured tool failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Tool(Protocol):
    """One validated operation registered with the tool runtime."""

    definition: ToolDefinition

    @property
    def input_model(self) -> type[BaseModel]:
        """Return the boundary validation model."""

    async def execute(self, arguments: BaseModel) -> JsonValue:
        """Execute with already validated arguments."""


@runtime_checkable
class RunScopedTool(Protocol):
    """Optional execution interface: identity is trusted executor data, not model input."""

    async def execute_in_run(
        self, arguments: BaseModel, *, run_id: str, step_id: int
    ) -> JsonValue: ...


@runtime_checkable
class PreparedTool(Protocol):
    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        """Validate execution targets and return scope plus a safe action preview."""


@dataclass(frozen=True, slots=True)
class ToolBundle:
    """Plugin service holding tools for the standard execution pipeline."""

    tools: tuple[Tool, ...]


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Auditable allow/deny result."""

    allowed: bool
    reason: str


@runtime_checkable
class PermissionAuthorizer(Protocol):
    """Replaceable permission decision service."""

    def authorize(self, definition: ToolDefinition) -> PermissionDecision:
        """Return an auditable decision for one validated call."""


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Default-deny logical capability policy."""

    allowed_permissions: frozenset[str] = field(default_factory=frozenset)
    denied_tools: frozenset[str] = field(default_factory=frozenset)

    def authorize(self, definition: ToolDefinition) -> PermissionDecision:
        """Deny named tools first, then require every declared capability."""
        if definition.name in self.denied_tools:
            return PermissionDecision(
                allowed=False, reason=f"tool explicitly denied: {definition.name}"
            )
        missing = set(definition.permissions) - self.allowed_permissions
        if missing:
            return PermissionDecision(
                allowed=False, reason=f"missing permissions: {', '.join(sorted(missing))}"
            )
        return PermissionDecision(allowed=True, reason="all required permissions granted")


class ToolRegistry:
    """Explicit tool registry with duplicate-name validation."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a uniquely named tool."""
        name = tool.definition.name
        if name in self._tools:
            msg = f"tool already registered: {name}"
            raise ValueError(msg)
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """Return a tool by model-visible name."""
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return definitions in stable name order."""
        return tuple(self._tools[name].definition for name in sorted(self._tools))


class LocalArtifactStore:
    """Persist oversized observations outside normal model context."""

    def __init__(self, workspace: Path) -> None:
        self._root = workspace.resolve(strict=True) / ".minidsh" / "artifacts"

    def write(self, run_id: str, content: str) -> str:
        """Write one UTF-8 artifact and return a workspace-relative path."""
        destination = self._root / run_id / f"{uuid.uuid4().hex}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination.relative_to(self._root.parent.parent).as_posix()


class ToolRuntime:
    """Validate, authorize, execute, retry, and normalize tool calls."""

    def __init__(  # noqa: PLR0913 - all policy dependencies are explicit
        self,
        registry: ToolRegistry,
        permissions: PermissionAuthorizer,
        events: EventSink,
        *,
        artifacts: LocalArtifactStore | None = None,
        max_output_chars: int = 20_000,
        max_retries: int = 1,
        normalize_results: bool = True,
        approvals: ApprovalProvider | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._events = events
        self._artifacts = artifacts
        self._max_output_chars = max_output_chars
        self._max_retries = max_retries
        self._normalize_results = normalize_results
        self._approvals = approvals
        self._hooks = hooks or HookRegistry()
        self._hooks.bind_events(events)

    def bind_approvals(self, provider: ApprovalProvider) -> None:
        """Bind a trusted presentation adapter before a run starts."""
        self._approvals = provider

    def release_run(self, run_id: str) -> None:
        release = getattr(self._approvals, "release", None)
        if callable(release):
            release(run_id)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return registered model-visible definitions."""
        return self._registry.definitions()

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        """Establish auditable identity for Loop and standalone tool callers."""
        if tool_hook_active():
            raise RuntimeError("tool invocation is forbidden in this hook phase")
        with self._hooks.tool_scope(self._hooks.tool_view(run_id, step_id)):
            return await self._execute(call, run_id, step_id)

    async def _execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:  # noqa: PLR0911, PLR0912, PLR0915 - guarded execution stages
        """Run the complete tool pipeline and never leak an exception to the loop."""
        if tool_hook_active():
            raise RuntimeError("recursive tool invocation from tool hook")
        started = time.monotonic()
        original = call
        try:
            call = await self._hooks.call(call)
            call = await self._hooks.transform_call(call, self._hooks.tool_view(run_id, step_id))
        except Exception:
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.DENIED,
                "hook_rejected",
                "tool hook rejected the call",
            )
        if call != original:
            await self._events.publish(
                Event(
                    "tool_call_rewritten",
                    run_id,
                    step_id,
                    {
                        "before": original.arguments,
                        "after": call.arguments,
                        "tool_before": original.name,
                        "tool_after": call.name,
                    },
                )
            )
        tool = self._registry.get(call.name)
        if tool is None:
            return await self._reject(
                call, started, run_id, step_id, ToolStatus.INVALID, "unknown_tool", "unknown tool"
            )
        try:
            arguments = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.INVALID,
                "invalid_arguments",
                str(exc),
            )

        decision = self._permissions.authorize(tool.definition)
        await self._events.publish(
            Event(
                "permission_decided",
                run_id,
                step_id,
                {
                    "tool": call.name,
                    "call_id": call.call_id,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                },
            )
        )
        if not decision.allowed:
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.DENIED,
                "permission_denied",
                decision.reason,
            )

        try:
            if (
                self._approvals is not None
                and tool.definition.side_effects
                and tool.definition.approval_required
            ):
                scope, preview = await self._prepare(tool, arguments, call)
                request = ApprovalRequest(
                    uuid.uuid4().hex, run_id, step_id, call, redact_text(preview)[:20_000], scope
                )
                await self._events.publish(
                    Event("approval_requested", run_id, step_id, request.payload())
                )
                try:
                    async with asyncio.timeout(120):
                        choice = await self._approvals.approve(request)
                except (TimeoutError, EOFError):
                    choice = ApprovalChoice.DENY
                await self._events.publish(
                    Event(
                        "approval_decided",
                        run_id,
                        step_id,
                        {
                            "request_id": request.request_id,
                            "call_id": call.call_id,
                            "choice": choice.value,
                        },
                    )
                )
                if choice is ApprovalChoice.DENY:
                    return await self._reject(
                        call,
                        started,
                        run_id,
                        step_id,
                        ToolStatus.DENIED,
                        "approval_denied",
                        "action was not approved",
                    )
                await self._prepare(tool, arguments, call)
                if not self._permissions.authorize(tool.definition).allowed:
                    raise PermissionError("permission changed before execution")
        except ToolError as exc:
            denied = exc.code in {"file_backend_denied", "sandbox_violation", "permission_denied"}
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.DENIED if denied else ToolStatus.FAILED,
                exc.code,
                redact_text(str(exc)),
            )
        except (PermissionError, ValueError) as exc:
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.DENIED,
                "preflight_denied",
                redact_text(str(exc)),
            )
        except OSError as exc:
            return await self._reject(
                call,
                started,
                run_id,
                step_id,
                ToolStatus.FAILED,
                "preflight_failed",
                redact_text(str(exc)),
            )

        await self._events.publish(
            Event(
                "tool_call_started",
                run_id,
                step_id,
                {"tool": call.name, "call_id": call.call_id, "arguments": call.arguments},
            )
        )
        attempts = 1 + (0 if tool.definition.side_effects else self._max_retries)
        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(tool.definition.timeout_seconds):
                    if isinstance(tool, RunScopedTool):
                        result = await tool.execute_in_run(
                            arguments, run_id=run_id, step_id=step_id
                        )
                    else:
                        result = await tool.execute(arguments)
                observation = self._success(call, run_id, result, started)
                try:
                    await self._hooks.observed(observation)
                except Exception:
                    await self._events.publish(
                        Event(
                            "hook_failed",
                            run_id,
                            step_id,
                            {"phase": "after_tool", "call_id": call.call_id},
                        )
                    )
                    observation = replace(
                        observation,
                        status=ToolStatus.FAILED,
                        error=ErrorInfo(
                            "hook_failed",
                            "post-execution hook failed",
                            {"execution_succeeded": True},
                        ),
                        retryable=False,
                    )
                await self._publish_completed(observation, run_id, step_id, attempt)
                return observation
            except TimeoutError:
                observation = self._failure(
                    call,
                    started,
                    ToolStatus.TIMED_OUT,
                    "tool_timeout",
                    "tool execution timed out",
                    retryable=not tool.definition.side_effects,
                )
            except ToolError as exc:
                observation = self._failure(
                    call,
                    started,
                    ToolStatus.FAILED,
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                )
            except Exception:
                observation = self._failure(
                    call,
                    started,
                    ToolStatus.FAILED,
                    "internal_tool_error",
                    "tool failed with an internal error",
                )
            if not observation.retryable or attempt == attempts:
                await self._publish_completed(observation, run_id, step_id, attempt)
                return observation
        raise AssertionError("unreachable tool retry state")

    @staticmethod
    async def _prepare(tool: Tool, arguments: BaseModel, call: ToolCall) -> tuple[str, str]:
        if isinstance(tool, PreparedTool):
            scope, preview = await tool.prepare(arguments)
            return f"{call.name}:{scope}", preview
        encoded = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
        return f"{call.name}:{encoded}", f"{call.name} {encoded}"

    def _success(
        self, call: ToolCall, run_id: str, result: JsonValue, started: float
    ) -> ToolObservation:
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        summary = summarize(call.name, result) if self._normalize_results else ""
        status = (
            ToolStatus.TIMED_OUT
            if isinstance(result, dict) and result.get("timed_out") is True
            else ToolStatus.SUCCEEDED
        )
        if len(serialized) <= self._max_output_chars:
            return ToolObservation(
                call.call_id,
                call.name,
                status,
                result,
                duration_ms=(time.monotonic() - started) * 1_000,
                summary=summary,
                effective_call=deepcopy(call),
            )
        artifacts: tuple[str, ...] = ()
        if self._artifacts is not None:
            artifacts = (self._artifacts.write(run_id, serialized),)
        preview = serialized[: max(0, self._max_output_chars - 1_500)]
        extracted = diagnostics(result) if self._normalize_results else []
        process_status: dict[str, JsonValue] = {}
        if (
            isinstance(result, dict)
            and type(result.get("exit_code")) is int
            and type(result.get("timed_out")) is bool
        ):
            # Truncation must not erase the outcome consumed by controlled finish checks.
            process_status = {"exit_code": result["exit_code"], "timed_out": result["timed_out"]}
            extracted = diagnostics(result)
        truncated: dict[str, JsonValue] = {}
        while True:
            truncated = {
                **process_status,
                "summary": summary,
                "diagnostics": cast("list[JsonValue]", extracted),
                "preview": preview,
                "original_characters": len(serialized),
            }
            if (
                len(json.dumps(truncated, ensure_ascii=False, sort_keys=True))
                <= self._max_output_chars
            ):
                break
            if preview:
                preview = preview[:-100]
            elif extracted:
                extracted.pop()
            else:
                break
        bounded_result: JsonValue = truncated
        if len(json.dumps(truncated, ensure_ascii=False, sort_keys=True)) > self._max_output_chars:
            bounded_result = 0
        return ToolObservation(
            call.call_id,
            call.name,
            status,
            bounded_result,
            duration_ms=(time.monotonic() - started) * 1_000,
            truncated=True,
            artifacts=artifacts,
            summary=summary,
            effective_call=deepcopy(call),
        )

    @staticmethod
    def _failure(  # noqa: PLR0913 - normalized error fields remain explicit
        call: ToolCall,
        started: float,
        status: ToolStatus,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> ToolObservation:
        return ToolObservation(
            call.call_id,
            call.name,
            status,
            error=ErrorInfo(code, message),
            duration_ms=(time.monotonic() - started) * 1_000,
            retryable=retryable,
            effective_call=deepcopy(call),
        )

    async def _reject(  # noqa: PLR0913, PLR0917 - normalized rejection fields stay explicit
        self,
        call: ToolCall,
        started: float,
        run_id: str,
        step_id: int,
        status: ToolStatus,
        code: str,
        message: str,
    ) -> ToolObservation:
        """Publish a completed observation for a call rejected before execution.

        Every tool call produces an observation and a durable event, so a rejection
        is auditable in the trace instead of only being implied by a missing event.
        """
        observation = self._failure(call, started, status, code, message)
        await self._publish_completed(observation, run_id, step_id, 1)
        return observation

    async def _publish_completed(
        self,
        observation: ToolObservation,
        run_id: str,
        step_id: int,
        attempt: int,
    ) -> None:
        await self._events.publish(
            Event(
                "tool_call_completed",
                run_id,
                step_id,
                {
                    "tool": observation.tool_name,
                    "call_id": observation.call_id,
                    "status": observation.status.value,
                    "duration_ms": observation.duration_ms,
                    "truncated": observation.truncated,
                    "retryable": observation.retryable,
                    "attempt": attempt,
                    "result": observation.result,
                    "error": (
                        {
                            "code": observation.error.code,
                            "message": observation.error.message,
                            "details": observation.error.details,
                        }
                        if observation.error is not None
                        else None
                    ),
                    "artifacts": list(observation.artifacts),
                },
            )
        )
        await self._hooks.observe_all(observation, self._hooks.tool_view(run_id, step_id))


def schema_for(model: type[BaseModel]) -> dict[str, JsonValue]:
    """Return a Pydantic schema narrowed to the domain JSON type."""
    return cast("dict[str, JsonValue]", model.model_json_schema())

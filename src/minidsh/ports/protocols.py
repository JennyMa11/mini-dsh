"""Ports implemented by runtime adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from minidsh.domain import (
    AgentBudget,
    ContextView,
    Event,
    Message,
    ModelPricing,
    ModelRequest,
    ModelResponse,
    RunResult,
    SessionMessageEvent,
    ToolCall,
    ToolDefinition,
    ToolObservation,
)
from minidsh.domain.extensions import FinishDecision, FinishProposal, HookView
from minidsh.domain.harness import ApprovalChoice, ApprovalRequest, PreparedContext, StreamEvent


@runtime_checkable
class Model(Protocol):
    """A replaceable language-model adapter."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate an action; raise domain.ModelCallError with any reported failure usage."""


@runtime_checkable
class StreamingModel(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield text deltas and one final response; close on cancellation."""


class ApprovalProvider(Protocol):
    async def approve(self, request: ApprovalRequest) -> ApprovalChoice:
        """Resolve one guarded action without expanding capabilities."""


class ExecutionHooks(Protocol):
    async def request(self, request: ModelRequest) -> ModelRequest: ...
    async def call(self, call: ToolCall) -> ToolCall: ...
    async def observed(self, observation: ToolObservation) -> None: ...
    async def finishing(self, result: RunResult) -> None: ...


@runtime_checkable
class ExtensionHooks(ExecutionHooks, Protocol):
    """V2 phases are opt-in; legacy execution hooks keep their existing signatures."""

    def tool_scope(self, state: HookView) -> AbstractContextManager[None]: ...
    async def started(self, state: HookView) -> None: ...
    async def model(self, request: ModelRequest, state: HookView) -> ModelRequest: ...
    async def transform_call(self, call: ToolCall, state: HookView) -> ToolCall: ...
    async def observe_all(self, observation: ToolObservation, state: HookView) -> None: ...
    async def step_ended(self, state: HookView) -> None: ...
    async def decide(self, proposal: FinishProposal) -> FinishDecision: ...
    async def ended(self, result: RunResult, *, timeout_seconds: float = 1.0) -> None: ...


@runtime_checkable
class ContextManager(Protocol):
    """Build a model view without mutating authoritative history."""

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        """Return a serializable context view."""


@runtime_checkable
class AdjustableContextBudget(Protocol):
    def with_counter(self, counter: Callable[[tuple[Message, ...]], int]) -> ContextManager: ...


@runtime_checkable
class PreparedContextManager(Protocol):
    async def prepare(  # noqa: PLR0913, PLR0917 - explicit preparation and accounting inputs
        self,
        history: tuple[Message, ...],
        task: str,
        run_id: str,
        step_id: int,
        max_calls: int,
        account: Callable[[int, float], None] | None = None,
    ) -> PreparedContext: ...


class ToolExecutor(Protocol):
    """Validate, authorize, and execute tool calls."""

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return model-visible tools."""

    async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
        """Execute one call and always return a structured observation."""


class AgentRunner(Protocol):
    """Run a single agent or an explicitly composed collaboration controller."""

    async def run(
        self, task: str, *, run_id: str | None = None, prior_history: tuple[Message, ...] = ()
    ) -> RunResult:
        """Run one budgeted task and return its terminal summary."""


@runtime_checkable
class EventSink(Protocol):
    """Receive runtime events without affecting core consistency."""

    async def publish(self, event: Event) -> None:
        """Publish one immutable event."""


@runtime_checkable
class EventStream(EventSink, Protocol):
    """Replaceable event publisher with explicit subscription lifecycle."""

    def subscribe(
        self, event_name: str, handler: Callable[[Event], Awaitable[None]]
    ) -> Callable[[], None]:
        """Subscribe and return an idempotent unsubscribe callback."""


@runtime_checkable
class SessionStore(Protocol):
    """Persist terminal run state independently of trace views."""

    async def save(self, result: RunResult) -> None:
        """Save or atomically replace one run record."""

    async def load(self, run_id: str) -> RunResult | None:
        """Load a run record when present."""


class SessionLog(Protocol):
    """Append-only history whose projection is the input to context policies."""

    async def append(self, step_id: int, message: Message) -> SessionMessageEvent:
        """Commit one message before it can become model-visible."""

    def messages(self) -> tuple[Message, ...]:
        """Project committed history without sharing mutable values."""

    def events(self) -> tuple[SessionMessageEvent, ...]:
        """Return the committed ordered facts."""


@runtime_checkable
class SessionJournal(Protocol):
    """Create new run logs and reopen existing facts without executing tools."""

    async def create(self, run_id: str) -> SessionLog:
        """Create an exclusive run log; never replace existing facts."""

    async def open(self, run_id: str) -> SessionLog:
        """Open an existing log or fail explicitly."""


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    """Explicit capabilities supplied to a replaceable loop factory."""

    model: Model
    context_manager: ContextManager
    tools: ToolExecutor
    events: EventSink
    budget: AgentBudget
    pricing: ModelPricing
    system_prompt: str
    reflection_enabled: bool
    detailed_tool_feedback: bool
    sessions: SessionStore
    journal: SessionJournal
    local_first_enabled: bool = False
    error_recovery_enabled: bool = True
    workspace_instructions_enabled: bool = False
    streaming_enabled: bool = False
    hooks: ExecutionHooks | None = None
    request_input_budget: int | None = None


@runtime_checkable
class AgentFactory(Protocol):
    """Construct a loop using only explicitly supplied capabilities."""

    def create(self, dependencies: AgentDependencies) -> AgentRunner:
        """Return a driver obeying the AgentRunner contract."""

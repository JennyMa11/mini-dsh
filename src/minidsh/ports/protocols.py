"""Ports implemented by runtime adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


@runtime_checkable
class Model(Protocol):
    """A replaceable language-model adapter."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate the next agent action."""


@runtime_checkable
class ContextManager(Protocol):
    """Build a model view without mutating authoritative history."""

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        """Return a serializable context view."""


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


@runtime_checkable
class AgentFactory(Protocol):
    """Construct a loop using only explicitly supplied capabilities."""

    def create(self, dependencies: AgentDependencies) -> AgentRunner:
        """Return a driver obeying the AgentRunner contract."""

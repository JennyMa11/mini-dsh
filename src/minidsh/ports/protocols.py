"""Ports implemented by runtime adapters."""

from __future__ import annotations

from typing import Protocol

from minidsh.domain import (
    ContextView,
    Event,
    Message,
    ModelRequest,
    ModelResponse,
    RunResult,
    ToolCall,
    ToolDefinition,
    ToolObservation,
)


class Model(Protocol):
    """A replaceable language-model adapter."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate the next agent action."""


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


class EventSink(Protocol):
    """Receive runtime events without affecting core consistency."""

    async def publish(self, event: Event) -> None:
        """Publish one immutable event."""


class SessionStore(Protocol):
    """Persist terminal run state independently of trace views."""

    async def save(self, result: RunResult) -> None:
        """Save or atomically replace one run record."""

    async def load(self, run_id: str) -> RunResult | None:
        """Load a run record when present."""

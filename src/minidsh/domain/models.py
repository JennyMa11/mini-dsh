"""Serializable values shared by the agent loop and its adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class Role(StrEnum):
    """Message roles understood by model adapters."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunStatus(StrEnum):
    """Explicit states of one agent run."""

    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ToolStatus(StrEnum):
    """Normalized outcomes from tool execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model request to invoke one named tool."""

    call_id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Message:
    """One model-visible conversation item."""

    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Model-visible tool metadata and policy requirements."""

    name: str
    description: str
    input_schema: dict[str, JsonValue]
    version: str = "1.0.0"
    side_effects: bool = False
    permissions: tuple[str, ...] = ()
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported or estimated token usage and derived spend."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate usage and cost across model calls."""
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_usd + other.cost_usd,
        )


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token USD pricing used for deterministic cost accounting."""

    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0

    def __post_init__(self) -> None:
        if self.input_usd_per_million < 0 or self.output_usd_per_million < 0:
            msg = "pricing rates must be non-negative"
            raise ValueError(msg)

    def cost_usd(self, usage: TokenUsage) -> float:
        """Return the USD cost of one model call at this rate card."""
        return (
            usage.input_tokens * self.input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000

    @classmethod
    def free(cls) -> ModelPricing:
        """Return a zero-cost rate card for scripted/offline runs."""
        return cls()


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-neutral model request."""

    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Provider-neutral model response."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Safe structured error returned to the loop."""

    code: str
    message: str
    details: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """Normalized observation produced for every tool call."""

    call_id: str
    tool_name: str
    status: ToolStatus
    result: JsonValue = None
    error: ErrorInfo | None = None
    duration_ms: float = 0.0
    truncated: bool = False
    retryable: bool = False
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextView:
    """A derived model context; the original history remains authoritative."""

    messages: tuple[Message, ...]
    strategy: str
    version: str
    estimated_tokens: int
    omitted_messages: int = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """Hard limits applied to one run."""

    max_steps: int = 50
    max_tokens: int = 100_000
    max_tool_calls: int = 100
    max_duration_seconds: float = 900.0

    def __post_init__(self) -> None:
        values = (
            self.max_steps,
            self.max_tokens,
            self.max_tool_calls,
            self.max_duration_seconds,
        )
        if any(value <= 0 for value in values):
            msg = "all budget limits must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Event:
    """Versioned fact emitted by the runtime."""

    name: str
    run_id: str
    step_id: int
    data: dict[str, JsonValue] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Terminal summary of an agent run."""

    run_id: str
    status: RunStatus
    answer: str
    steps: int
    token_usage: TokenUsage
    tool_calls: int
    duration_ms: float
    failure_reason: str | None = None
    history: tuple[Message, ...] = ()

    @property
    def cost_usd(self) -> float:
        """Return the total model spend for this run."""
        return self.token_usage.cost_usd


def estimate_tokens(messages: tuple[Message, ...]) -> int:
    """Estimate model tokens deterministically without binding core code to a tokenizer.

    The estimator is deliberately simple and shared by context strategies and the
    scripted adapter, so an ablation measures the same quantity on both sides.
    """
    characters = sum(len(message.content) for message in messages)
    tool_characters = sum(
        len(call.name) + len(str(call.arguments))
        for message in messages
        for call in message.tool_calls
    )
    return max(1, (characters + tool_characters + 3) // 4)

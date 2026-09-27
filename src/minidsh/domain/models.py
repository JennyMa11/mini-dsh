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


class FailureCategory(StrEnum):
    """Closed vocabulary describing *where* a run failed.

    The categories are deliberately layered rather than ranked: a harness-level
    category (``permission_denied``, ``invalid_tool_call``) means the runtime did its
    job and the agent misused it, while ``model_reasoning_failure`` means the agent
    had every opportunity and still produced a wrong change. Keeping them disjoint is
    what makes "is the harness correct?" and "is the model capable?" separable
    questions instead of one blended success rate.
    """

    NONE = "none"
    MODEL_REASONING_FAILURE = "model_reasoning_failure"
    CONTEXT_MISS = "context_miss"
    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_TOOL_CALL = "invalid_tool_call"
    VERIFICATION_FAILED = "verification_failed"
    LOOP_STUCK = "loop_stuck"
    PROVIDER_ERROR = "provider_error"


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
    # Provider chain-of-thought, when the provider returns one.  DeepSeek requires
    # it to be echoed back on every request that carries ``tools``, and rejects the
    # request with a 400 otherwise; other providers simply never set it.
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class SessionMessageEvent:
    """An ordered model-history fact, independent of context strategy."""

    run_id: str
    sequence: int
    step_id: int
    message: Message
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"

    @property
    def name(self) -> str:
        """Return the factual event name for this message role."""
        return f"{self.message.role.value}_message"


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
    """Provider-reported or estimated token usage and derived spend.

    ``cached_input_tokens`` is a *subset* of ``input_tokens``, matching how
    OpenAI-compatible providers report prompt-cache hits. It is tracked separately
    because cached input is billed at a different rate on every major provider, and
    collapsing the two would make a real-model cost comparison wrong in the
    direction that flatters long-context strategies.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_input_tokens(self) -> int:
        """Return input tokens billed at the uncached rate."""
        return max(0, self.input_tokens - self.cached_input_tokens)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate usage and cost across model calls."""
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_usd + other.cost_usd,
            self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token USD pricing used for deterministic cost accounting."""

    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    cached_input_usd_per_million: float | None = None

    def __post_init__(self) -> None:
        rates = (self.input_usd_per_million, self.output_usd_per_million)
        cached = self.cached_input_usd_per_million
        if any(rate < 0 for rate in rates) or (cached is not None and cached < 0):
            msg = "pricing rates must be non-negative"
            raise ValueError(msg)

    def cost_usd(self, usage: TokenUsage) -> float:
        """Return the USD cost of one model call at this rate card.

        When no cached rate is declared the cached tokens fall back to the standard
        input rate, which reproduces the pre-cache cost model exactly.
        """
        cached_rate = (
            self.input_usd_per_million
            if self.cached_input_usd_per_million is None
            else self.cached_input_usd_per_million
        )
        return (
            usage.billable_input_tokens * self.input_usd_per_million
            + usage.cached_input_tokens * cached_rate
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000

    @classmethod
    def free(cls) -> ModelPricing:
        """Return a zero-cost rate card for scripted/offline runs."""
        return cls()


@dataclass(frozen=True, slots=True)
class LatencyBreakdown:
    """Wall-clock attribution across the three layers a harness can observe.

    ``harness_ms`` is the residual of the end-to-end run minus the time spent inside
    model calls and tool executions. It is the number that answers "how much does the
    runtime itself cost?", which is the only latency component the harness author can
    actually optimize.
    """

    model_ms: float = 0.0
    tool_ms: float = 0.0
    harness_ms: float = 0.0
    total_ms: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0

    @property
    def model_share(self) -> float:
        """Return the fraction of end-to-end latency spent waiting on the provider."""
        return self.model_ms / self.total_ms if self.total_ms > 0 else 0.0

    @property
    def overhead_share(self) -> float:
        """Return the fraction of end-to-end latency attributable to the harness."""
        return self.harness_ms / self.total_ms if self.total_ms > 0 else 0.0

    @property
    def mean_model_ms(self) -> float:
        """Return the mean latency of one model call."""
        return self.model_ms / self.model_calls if self.model_calls else 0.0

    def to_json(self) -> dict[str, JsonValue]:
        """Return a stable serialization for reports and traces."""
        return {
            "model_ms": round(self.model_ms, 3),
            "tool_ms": round(self.tool_ms, 3),
            "harness_ms": round(self.harness_ms, 3),
            "total_ms": round(self.total_ms, 3),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "model_share": round(self.model_share, 4),
            "overhead_share": round(self.overhead_share, 4),
            "mean_model_ms": round(self.mean_model_ms, 3),
        }


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
    reasoning_content: str = ""


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
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)

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

"""Versioned extension values and the deliberately narrow controlled-call contract."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from minidsh.domain.models import JsonValue, Message, ToolObservation

MAX_FINISH_FEEDBACK = 4096


@dataclass(frozen=True, slots=True)
class BudgetView:
    """Remaining allowance at a boundary, never the mutable run counters."""

    steps: int
    tokens: int
    tool_calls: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class HookView:
    """Detached facts for one hook invocation; None budget means standalone tool use."""

    run_id: str
    step_id: int
    remaining: BudgetView | None
    messages: tuple[Message, ...] = ()


class ExtensionBudgetExhaustedError(RuntimeError):
    """The controlled action was stopped before exceeding a run allowance."""


class ControlledCaller(Protocol):
    """A run-scoped, budgeted tool capability, without a raw executor or counters."""

    def snapshot(self) -> HookView:
        """Read the current detached budget and facts during the owning phase."""

    async def invoke(self, name: str, arguments: dict[str, JsonValue]) -> ToolObservation:
        """Invoke through the normal tool pipeline; unavailable outside its phase."""


class FinishAction(StrEnum):
    ALLOW = "allow"
    CONTINUE = "continue"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class FinishDecision:
    """A bounded decision; a continued proposal always explains what to repair."""

    action: FinishAction = FinishAction.ALLOW
    feedback: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.action, FinishAction):
            raise ValueError("invalid finish action")
        if len(self.feedback) > MAX_FINISH_FEEDBACK:
            raise ValueError("finish feedback exceeds 4096 characters")
        if self.action is FinishAction.ALLOW and self.feedback:
            raise ValueError("allow cannot contain feedback")
        if self.action is not FinishAction.ALLOW and not self.feedback.strip():
            raise ValueError("continue/fail requires feedback")


@dataclass(frozen=True, slots=True)
class FinishProposal:
    """Candidate completion; caller is valid only while checking this proposal."""

    view: HookView
    answer: str
    caller: ControlledCaller | None = None

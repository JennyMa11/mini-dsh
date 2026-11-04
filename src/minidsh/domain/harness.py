"""Provider-neutral interactive runtime contracts."""

from dataclasses import dataclass
from enum import StrEnum

from minidsh.domain.models import ContextView, JsonValue, ModelResponse, ToolCall


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """A display delta or the single settled response; deltas never execute tools."""

    text: str = ""
    response: ModelResponse | None = None


class ApprovalChoice(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REMEMBER = "remember"


@dataclass(frozen=True, slots=True)
class PreparedContext:
    view: ContextView
    tool_calls: int = 0
    tool_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    run_id: str
    step_id: int
    call: ToolCall
    preview: str
    scope: str

    def payload(self) -> dict[str, JsonValue]:
        """Return a bounded presentation payload, never a capability grant."""
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool": self.call.name,
            "preview": self.preview,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    latest_run_id: str | None
    turn_count: int
    status: str
    parent_session_id: str | None = None

    def payload(self) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "latest_run_id": self.latest_run_id,
            "turn_count": self.turn_count,
            "status": self.status,
            "parent_session_id": self.parent_session_id,
        }

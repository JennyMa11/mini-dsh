"""Durable, redacted session message facts in append-only JSONL."""

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from minidsh.adapters.session_codec import StoredMessage, message_to_json
from minidsh.domain import Message, Role, SessionMessageEvent, ToolCall, redact_value
from minidsh.domain.session import MemorySessionLog
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.ports import SessionLog


class _StoredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    run_id: str
    sequence: int
    step_id: int
    occurred_at: datetime
    message: StoredMessage

    def event(self) -> SessionMessageEvent:
        """Decode a validated event into provider-neutral contracts."""
        message = self.message
        return SessionMessageEvent(
            self.run_id,
            self.sequence,
            self.step_id,
            Message(
                Role(message.role),
                message.content,
                message.name,
                message.tool_call_id,
                tuple(ToolCall(c.call_id, c.name, c.arguments) for c in message.tool_calls),
                message.reasoning_content,
            ),
            self.occurred_at,
            self.schema_version,
        )


class JsonlSessionLog(MemorySessionLog):
    """Commit each bounded message write before updating its projection."""

    def __init__(self, run_id: str, path: Path, *, writable: bool = True) -> None:
        super().__init__(run_id)
        self._path = path
        self._writable = writable

    async def append(self, step_id: int, message: Message) -> SessionMessageEvent:
        """Append and fsync without an await between disk and memory commit."""
        if not self._writable:
            raise ValueError("reopened session logs are read-only")
        event = self.next_event(step_id, message)
        payload = redact_value(
            {
                "schema_version": event.schema_version,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "step_id": event.step_id,
                "occurred_at": event.occurred_at.isoformat(),
                "message": message_to_json(event.message),
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        committed = _StoredEvent.model_validate_json(encoded).event()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(committed)
        return deepcopy(committed)

    def restore(self, events: tuple[SessionMessageEvent, ...]) -> None:
        """Load validated facts only into a fresh handle."""
        if self._events:
            raise ValueError("session handle already contains events")
        for event in events:
            expected = self.next_event(event.step_id, event.message)
            if event.run_id != self.run_id or event.sequence != expected.sequence:
                raise ValueError("invalid session event run_id or sequence")
            if event.occurred_at.utcoffset() is None:
                raise ValueError("session timestamp must include a timezone")
            self._events.append(deepcopy(event))


class JsonlSessionJournal:
    """Exclusive run files; terminal JSON summaries remain compatible views."""

    def __init__(self, workspace: Path) -> None:
        self._sandbox = WorkspaceSandbox(workspace)
        self._logs: dict[str, JsonlSessionLog] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id or not run_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("run_id contains unsafe characters")
        return self._sandbox.resolve(f".minidsh/session_events/{run_id}.jsonl")

    async def create(self, run_id: str) -> SessionLog:
        """Claim a new file without replacing any old event log."""
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as exc:
            raise ValueError("session log already exists") from exc
        log = JsonlSessionLog(run_id, path)
        self._logs[run_id] = log
        return log

    async def open(self, run_id: str) -> SessionLog:
        """Reject incomplete/corrupt facts rather than guessing execution results."""
        if run_id in self._logs:
            return self._logs[run_id]
        path = self._path(run_id)
        log = read_session_log(path, run_id)
        self._logs[run_id] = log
        return log


def read_session_log(path: Path, run_id: str) -> JsonlSessionLog:
    """Reconstruct a read-only projection for journals and terminal readers."""
    contents = path.read_text(encoding="utf-8")
    if contents and not contents.endswith("\n"):
        raise ValueError("incomplete session event tail")
    log = JsonlSessionLog(run_id, path, writable=False)
    log.restore(
        tuple(_StoredEvent.model_validate_json(line).event() for line in contents.splitlines())
    )
    return log

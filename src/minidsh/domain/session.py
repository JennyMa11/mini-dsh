"""Pure ordered session facts and their message projection."""

from copy import deepcopy
from dataclasses import replace
from typing import cast

from minidsh.domain.models import JsonValue, Message, SessionMessageEvent
from minidsh.domain.redaction import redact_text, redact_value


class MemorySessionLog:
    """Ordered immutable snapshots; append is the only mutation operation."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[SessionMessageEvent] = []

    async def append(self, step_id: int, message: Message) -> SessionMessageEvent:
        """Commit an isolated copy of the model-history fact."""
        event = self.next_event(step_id, message)
        self._events.append(event)
        return deepcopy(event)

    def next_event(self, step_id: int, message: Message) -> SessionMessageEvent:
        """Validate ordering and prepare a fact without committing it."""
        if step_id < 0 or (self._events and step_id < self._events[-1].step_id):
            raise ValueError("session step_id must be monotonic")
        return SessionMessageEvent(
            self.run_id, len(self._events) + 1, step_id, sanitize_message(message)
        )

    def messages(self) -> tuple[Message, ...]:
        """Project messages from facts, never from a second transcript."""
        return tuple(deepcopy(event.message) for event in self._events)

    def events(self) -> tuple[SessionMessageEvent, ...]:
        """Return independent snapshots of the committed facts."""
        return deepcopy(tuple(self._events))


def sanitize_message(message: Message) -> Message:
    """Apply the same credential policy to memory and durable history."""
    return replace(
        message,
        content=redact_text(message.content),
        name=redact_text(message.name) if message.name is not None else None,
        reasoning_content=(
            redact_text(message.reasoning_content)
            if message.reasoning_content is not None
            else None
        ),
        tool_calls=tuple(
            replace(
                call, arguments=cast("dict[str, JsonValue]", redact_value(deepcopy(call.arguments)))
            )
            for call in message.tool_calls
        ),
    )

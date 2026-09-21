"""Failure-isolating asynchronous event bus."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from minidsh.domain import Event

type EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubscriberError:
    """A subscriber failure retained for diagnostics."""

    event_name: str
    handler_name: str
    error: str


@dataclass(slots=True)
class EventBus:
    """Dispatch events while preventing subscribers from breaking the runtime."""

    _subscribers: dict[str, list[EventHandler]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )
    _errors: list[SubscriberError] = field(default_factory=list, init=False)

    def subscribe(self, event_name: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe to one event name or ``*`` and return an unsubscribe callback."""
        handlers = self._subscribers[event_name]
        handlers.append(handler)

        def unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Publish in registration order and isolate subscriber exceptions."""
        handlers = [*self._subscribers.get(event.name, ()), *self._subscribers.get("*", ())]
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:
                self._errors.append(
                    SubscriberError(event.name, self._handler_name(handler), repr(exc))
                )

    @property
    def errors(self) -> tuple[SubscriberError, ...]:
        """Return subscriber failures observed so far."""
        return tuple(self._errors)

    @staticmethod
    def _handler_name(handler: EventHandler) -> str:
        return getattr(handler, "__qualname__", type(handler).__qualname__)

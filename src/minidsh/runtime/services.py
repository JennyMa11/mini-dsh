"""Explicit service registry used during runtime composition."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class ServiceError(RuntimeError):
    """Raised for invalid service registration or resolution."""


class ServiceRegistry:
    """A small typed registry; services are registered only by composition code."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}
        self._cleanups: list[Callable[[], None]] = []

    def on_cleanup(self, callback: Callable[[], None]) -> None:
        """Own subscriptions/hook registrations for reverse-order plugin teardown."""
        self._cleanups.append(callback)

    def cleanup_from(self, offset: int) -> None:
        errors: list[Exception] = []
        while len(self._cleanups) > offset:
            try:
                self._cleanups.pop()()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ServiceError("plugin cleanup callback failed") from errors[0]

    @property
    def cleanup_count(self) -> int:
        return len(self._cleanups)

    def register(self, key: str, service: object) -> None:
        """Register exactly one service for a stable key."""
        if not key:
            msg = "service key cannot be empty"
            raise ServiceError(msg)
        if key in self._services:
            msg = f"service already registered: {key}"
            raise ServiceError(msg)
        self._services[key] = service

    def get(self, key: str, expected_type: type[T]) -> T:
        """Resolve a service and validate its runtime type."""
        try:
            service = self._services[key]
        except KeyError as exc:
            msg = f"service not found: {key}"
            raise ServiceError(msg) from exc
        if not isinstance(service, expected_type):
            msg = f"service {key!r} is not {expected_type.__qualname__}"
            raise ServiceError(msg)
        return service

    def contains(self, key: str) -> bool:
        """Return whether a key is registered."""
        return key in self._services

    def unregister(self, key: str) -> None:
        """Remove a service during controlled plugin shutdown."""
        self._services.pop(key, None)

    @property
    def keys(self) -> frozenset[str]:
        """Return a stable snapshot of registered keys."""
        return frozenset(self._services)

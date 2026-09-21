"""Append-only, redacted JSONL runtime trace."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minidsh.domain import Event

_SECRET_KEYS = frozenset(
    {"api_key", "authorization", "credential", "password", "secret", "access_token"}
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|secret|access[_-]?token)\s*([:=])\s*([^\s,;]+)"
)


@dataclass(slots=True)
class JsonlTraceWriter:
    """Serialize facts one per line while redacting common secret fields."""

    path: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def __call__(self, event: Event) -> None:
        """Allow direct registration as an EventBus subscriber."""
        await self.append(event)

    async def append(self, event: Event) -> None:
        """Append one durable event without blocking the event loop."""
        record = {
            "schema_version": event.schema_version,
            "name": event.name,
            "run_id": event.run_id,
            "step_id": event.step_id,
            "occurred_at": event.occurred_at.isoformat(),
            "data": self._redact(event.data),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    @classmethod
    def _redact(cls, value: Any, *, key: str = "") -> Any:
        if key.lower() in _SECRET_KEYS:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): cls._redact(item, key=str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            without_bearer = _BEARER.sub("Bearer [REDACTED]", value)
            return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", without_bearer)
        return value

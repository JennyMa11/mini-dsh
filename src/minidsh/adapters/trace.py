"""Append-only, redacted JSONL runtime trace."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from minidsh.domain import Event, redact_value


@dataclass(slots=True)
class JsonlTraceWriter:
    """Serialize facts one per line while redacting common secret fields.

    ``truncate_on_start`` makes the writer own the lifetime of the file: the first
    append opens it with ``"w"`` and every later append with ``"a"``. A caller that
    wants a fresh trace therefore never has to delete a stale one first, which removes
    both a delete-then-create window and any chance of appending a second run onto the
    first run's facts.
    """

    path: Path
    truncate_on_start: bool = False
    _started: bool = field(default=False, init=False)
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
            "data": redact_value(event.data),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._lock:
            mode = "w" if self.truncate_on_start and not self._started else "a"
            self._started = True
            await asyncio.to_thread(self._append_sync, line, mode)

    def _append_sync(self, line: str, mode: str = "a") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open(mode, encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

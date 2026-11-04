"""Pure, bounded fingerprints of known raw material; never a store of source text."""

from __future__ import annotations

import hashlib
import json
import re

from minidsh.domain.memory import (
    MAX_FORBIDDEN_MEMORY_SOURCES,
    MAX_MEMORY_BYTES,
    MemoryValidationError,
)
from minidsh.domain.models import JsonValue, Message, Role


def _fingerprints(text: str) -> set[str]:
    try:
        raw = text.encode("utf-8")
        if not raw or len(raw) > MAX_MEMORY_BYTES:
            return set()
        values = {
            hashlib.sha256(raw).hexdigest(),
            hashlib.sha256(text.strip().encode()).hexdigest(),
        }
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                canonical = json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                values.add(hashlib.sha256(canonical).hexdigest())
        except (ValueError, RecursionError):
            pass
        return values
    except UnicodeError:
        raise MemoryValidationError("memory_content_invalid") from None


class MemoryContentPolicy:
    """Exact/trimmed text and canonical JSON matches; no semantic leakage detector."""

    def __init__(self, forbidden_sha256: tuple[str, ...] = ()) -> None:
        if len(forbidden_sha256) > MAX_FORBIDDEN_MEMORY_SOURCES or any(
            re.fullmatch(r"[a-f0-9]{64}", value) is None for value in forbidden_sha256
        ):
            raise MemoryValidationError("memory_config_invalid")
        self._forbidden = frozenset(forbidden_sha256)
        self._observed: set[str] = set()
        self._limited = False
        self._invalid = False

    def copy(self) -> MemoryContentPolicy:
        result = MemoryContentPolicy(tuple(self._forbidden))
        result._observed = self._observed.copy()
        result._limited = self._limited
        result._invalid = self._invalid
        return result

    def observe(self, messages: tuple[Message, ...]) -> None:
        for message in messages:
            if message.role is Role.ASSISTANT or (
                message.role is Role.USER and message.name == "project_memory"
            ):
                continue
            pending: list[JsonValue] = [message.content, *message.content.splitlines()]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str):
                    try:
                        self._observed.update(_fingerprints(value))
                    except MemoryValidationError:
                        self._invalid = True
                        raise
                    if len(self._observed) > MAX_FORBIDDEN_MEMORY_SOURCES:
                        self._limited = True
                        return
                    try:
                        decoded: JsonValue = json.loads(value)
                    except (ValueError, RecursionError):
                        continue
                    if decoded != value:
                        pending.append(decoded)

    def check(self, content: str) -> None:
        if self._invalid:
            raise MemoryValidationError("memory_content_invalid")
        values = _fingerprints(content)
        if values & self._forbidden:
            raise MemoryValidationError("memory_forbidden_content")
        if self._limited:
            raise MemoryValidationError("memory_source_guard_limit")
        if values & self._observed:
            raise MemoryValidationError("memory_raw_material")

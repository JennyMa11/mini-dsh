"""Immutable audit pages and explicit physical-schema migration receipts."""

from dataclasses import dataclass

from minidsh.domain.memory import MemoryEntry

MAX_HISTORY_PAGE = 100


@dataclass(frozen=True, slots=True)
class MemoryMigration:
    changed: bool
    from_version: int | None
    to_version: int
    backup_name: str | None = None
    backup_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryHistoryRecord:
    sequence: int
    operation: str
    before: MemoryEntry | None
    after: MemoryEntry


@dataclass(frozen=True, slots=True)
class MemoryHistoryPage:
    records: tuple[MemoryHistoryRecord, ...]
    next_after: int | None

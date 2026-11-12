"""Project-scoped persistence; never a model-visible database or filesystem handle."""

from typing import Protocol, runtime_checkable

from minidsh.domain import Message
from minidsh.domain.memory import MemoryEntry, MemoryEvidence, MemorySelection
from minidsh.domain.memory_history import MemoryHistoryPage, MemoryMigration


class MemoryStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class MemoryStore(Protocol):
    async def create(self, entry: MemoryEntry) -> MemoryEntry: ...
    async def get(self, project_id: str, entry_id: str) -> MemoryEntry | None: ...
    async def snapshot(self, project_id: str) -> tuple[MemoryEntry, ...]: ...
    async def replace(self, expected: MemoryEntry, updated: MemoryEntry) -> MemoryEntry: ...
    async def close(self) -> None: ...


@runtime_checkable
class MemoryHistoryStore(MemoryStore, Protocol):
    async def history_enabled(self) -> bool: ...
    async def migrate_history(self) -> MemoryMigration: ...
    async def history(
        self, project_id: str, entry_id: str, *, after_sequence: int = 0, limit: int = 20
    ) -> MemoryHistoryPage: ...
    async def revision(
        self, project_id: str, entry_id: str, revision: int, proposal_sha256: str
    ) -> MemoryEntry | None: ...
    async def replace_many(
        self, changes: tuple[tuple[MemoryEntry, MemoryEntry], ...]
    ) -> tuple[MemoryEntry, ...]: ...


class MemoryEvidenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class MemoryEvidenceSource(Protocol):
    async def resolve(self, evidence_id: str) -> MemoryEvidence: ...
    async def resolve_sha256(self, sha256: str) -> MemoryEvidence: ...


class MemoryView(Protocol):
    def selection_for(
        self,
        run_id: str,
        *,
        paths: tuple[str, ...] = (),
        error_type: str | None = None,
    ) -> MemorySelection | None: ...


class ProgressiveMemoryView(Protocol):
    def observe_history(self, history: tuple[Message, ...]) -> None: ...

    def messages_for(self, run_id: str) -> tuple[Message, ...]: ...

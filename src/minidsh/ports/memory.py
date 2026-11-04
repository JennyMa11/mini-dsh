"""Project-scoped persistence; never a model-visible database or filesystem handle."""

from typing import Protocol, runtime_checkable

from minidsh.domain.memory import MemoryEntry, MemoryEvidence, MemorySelection


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

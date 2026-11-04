"""Read-only skill views consumed by Context; no file or load capability."""

from typing import Protocol

from minidsh.domain.skills import SkillCatalog, SkillDocument


class SkillView(Protocol):
    def catalog_for(self, run_id: str) -> SkillCatalog | None: ...
    def active_for(self, run_id: str) -> tuple[SkillDocument, ...]: ...

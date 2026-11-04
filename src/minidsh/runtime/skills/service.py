"""Run-owned skill snapshots, accessed for I/O only by permissioned tools."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.domain.skills import (
    MAX_ACTIVE_SKILLS,
    MAX_SKILLS,
    SkillCatalog,
    SkillDocument,
    SkillRun,
    SkillValidationError,
    parse_skill,
)
from minidsh.ports.files import FileBackend
from minidsh.tools.runtime import ToolError


class _SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    content: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Discovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    documents: list[_SourceDocument] = Field(max_length=MAX_SKILLS)


def _document(source: _SourceDocument) -> SkillDocument:
    try:
        value = parse_skill(source.path, source.content.encode("utf-8"))
    except UnicodeError:
        raise ToolError("skill_source_invalid", "skill_source_invalid") from None
    except SkillValidationError as exc:
        raise ToolError(exc.code, exc.code) from None
    if value.metadata.sha256 != source.sha256:
        raise ToolError("skill_source_invalid", "skill_source_invalid")
    return value


class SkillService:
    """Explicitly injected file capability; no global state or context-driven I/O."""

    def __init__(self, files: FileBackend, roots: tuple[str, ...]) -> None:
        self._files = files
        self._roots = tuple(roots)
        self._runs: dict[str, SkillRun] = {}
        self._preparing: dict[str, object] = {}

    async def discover(self, run_id: str) -> SkillCatalog:
        if run_id in self._runs or run_id in self._preparing:
            raise ToolError("skills_already_prepared", "skills_already_prepared")
        token = object()
        self._preparing[run_id] = token
        try:
            raw = await self._files.invoke("skill_scan", {"roots": list(self._roots)})
            try:
                parsed = _Discovery.model_validate(raw)
            except ValidationError:
                raise ToolError("skill_source_invalid", "skill_source_invalid") from None
            documents = tuple(_document(source) for source in parsed.documents)
            try:
                catalog = SkillCatalog.build(documents)
            except SkillValidationError as exc:
                raise ToolError(exc.code, exc.code) from None
            if self._preparing.get(run_id) is not token:
                raise ToolError("skills_run_closed", "skills_run_closed")
            self._runs[run_id] = SkillRun(catalog)
            return catalog
        finally:
            if self._preparing.get(run_id) is token:
                self._preparing.pop(run_id, None)

    async def load(self, run_id: str, name: str) -> SkillDocument:
        state = self._runs.get(run_id)
        if state is None:
            raise ToolError("skills_not_prepared", "skills_not_prepared")
        try:
            source = state.source(name)
        except SkillValidationError as exc:
            raise ToolError(exc.code, exc.code) from None
        if len(state.active) >= MAX_ACTIVE_SKILLS and all(
            d.metadata.name != name for d in state.active
        ):
            raise ToolError("skill_activation_limit", "skill_activation_limit")
        raw = await self._files.invoke(
            "skill_read", {"path": source.path, "expected_sha256": source.sha256}
        )
        try:
            document = _document(_SourceDocument.model_validate(raw))
        except ValidationError:
            raise ToolError("skill_source_invalid", "skill_source_invalid") from None
        if self._runs.get(run_id) is not state:
            raise ToolError("skills_run_closed", "skills_run_closed")
        if document.metadata != source:
            raise ToolError("skill_changed", "skill_changed")
        try:
            state.activate(document)
        except SkillValidationError as exc:
            raise ToolError(exc.code, exc.code) from None
        return document

    def catalog_for(self, run_id: str) -> SkillCatalog | None:
        state = self._runs.get(run_id)
        return state.catalog if state is not None else None

    def active_for(self, run_id: str) -> tuple[SkillDocument, ...]:
        state = self._runs.get(run_id)
        return state.active if state is not None else ()

    def release(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        self._preparing.pop(run_id, None)

    def clear(self) -> None:
        self._runs.clear()
        self._preparing.clear()

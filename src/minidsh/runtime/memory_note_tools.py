"""Typed proposals and separate, explicit operator-only structured note tools."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minidsh.domain import JsonValue, ToolDefinition
from minidsh.domain.memory import MemoryEntry, MemoryKind
from minidsh.domain.memory_notes import MemoryNote, MemoryNoteSource
from minidsh.runtime.memory import MemoryService
from minidsh.runtime.memory_maintenance import memory_entry_receipt, validate_memory_operator_id
from minidsh.tools.runtime import Tool, ToolError, schema_for


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class MemoryNoteSourceInput(_Input):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    revision: int = Field(gt=0)
    proposal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_run_id: str = Field(min_length=1, max_length=128)

    def source(self) -> MemoryNoteSource:
        return MemoryNoteSource(
            self.entry_id,
            self.revision,
            self.proposal_sha256,
            self.evidence_sha256,
            self.source_run_id,
        )


class _Note(_Input):
    trigger: str = Field(min_length=1, max_length=256)
    rule: str = Field(min_length=1, max_length=1024)
    verification: str = Field(min_length=1, max_length=256)
    exceptions: list[str] = Field(default_factory=list, max_length=4)
    sources: list[MemoryNoteSourceInput] = Field(default_factory=list, max_length=5)
    supersedes: list[MemoryNoteSourceInput] = Field(default_factory=list, max_length=5)

    def note(self) -> MemoryNote:
        return MemoryNote(
            self.trigger,
            self.rule,
            self.verification,
            tuple(self.exceptions),
            tuple(item.source() for item in self.sources),
            tuple(item.source() for item in self.supersedes),
        )

    @model_validator(mode="after")
    def domain_bounds(self) -> Self:
        note = self.note()
        MemoryEntry.propose(
            "schema_entry",
            "schema_scope",
            MemoryKind.ENVIRONMENT,
            topic="schema_topic",
            content=note.to_content(),
            source_run_id=getattr(self, "source_run_id", "schema_run"),
            applicable_version="v1",
            paths=tuple(getattr(self, "paths", ())),
            error_type=getattr(self, "error_type", None),
        )
        return self


class _Proposal(_Note):
    kind: Literal["environment", "repair", "diagnosis"]
    topic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    paths: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=32
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=128)


class _Merge(_Proposal):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    source_run_id: str = Field(min_length=1, max_length=128)


class _Revision(_Note):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    expected_revision: int = Field(gt=0)
    source_run_id: str = Field(min_length=1, max_length=128)


class _History(_Input):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    after_sequence: int = Field(default=0, ge=0)
    # One complete state transition per page; Tool Runtime retains the final output cap.
    limit: int = Field(default=1, ge=1, le=1)


def _bound(service: MemoryService, base: type[_Note]) -> type[_Note]:
    class Checked(base):  # type: ignore[valid-type,misc]  # runtime-selected Pydantic boundary
        @model_validator(mode="after")
        def content_policy(self) -> Self:
            service.check_note_content(self.note())
            return self

    return Checked


def memory_note_proposal_definition() -> ToolDefinition:
    return ToolDefinition(
        "propose_memory_note",
        "Propose a structured note; lineage is not validation or approval.",
        schema_for(_Proposal),
        version="1.0.0",
        side_effects=True,
        permissions=("memory.propose", "memory.read", "memory.evidence.read"),
        timeout_seconds=5.0,
        approval_required=False,
    )


class ProposeMemoryNoteTool:
    def __init__(self, service: MemoryService) -> None:
        self.input_model = _bound(service, _Proposal)
        self.definition = memory_note_proposal_definition()
        self._service = service

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        raise ToolError("memory_run_required", "memory_run_required")

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        del step_id
        parsed = _Proposal.model_validate(arguments.model_dump())
        entry = await self._service.propose_note(
            run_id,
            kind=MemoryKind(parsed.kind),
            topic=parsed.topic,
            note=parsed.note(),
            paths=tuple(parsed.paths),
            error_type=parsed.error_type,
        )
        return memory_entry_receipt(entry, content=False)


class _OperatorTool:
    def __init__(
        self,
        definition: ToolDefinition,
        model: type[BaseModel],
        action: Callable[[BaseModel], Awaitable[JsonValue]],
    ) -> None:
        self.definition, self.input_model, self._action = definition, model, action

    async def execute(self, arguments: BaseModel) -> JsonValue:
        return await self._action(arguments)


def memory_note_maintenance_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            "migrate_memory_history",
            "Explicit outside-run migration with a private backup.",
            schema_for(_Input),
            side_effects=True,
            permissions=("memory.maintain", "memory.migrate"),
            approval_required=False,
        ),
        ToolDefinition(
            "review_memory_history",
            "Review one private state transition outside runs.",
            schema_for(_History),
            permissions=("memory.maintain",),
            approval_required=False,
        ),
        ToolDefinition(
            "merge_memory_notes",
            "Create an unverified structured draft outside runs.",
            schema_for(_Merge),
            side_effects=True,
            permissions=("memory.maintain", "memory.evidence.read"),
            approval_required=False,
        ),
        ToolDefinition(
            "revise_memory_note",
            "Revise all note fields, invalidating old authority.",
            schema_for(_Revision),
            side_effects=True,
            permissions=("memory.maintain", "memory.evidence.read"),
            approval_required=False,
        ),
    )


def memory_note_maintenance_tools(service: MemoryService, *, operator_id: str) -> tuple[Tool, ...]:
    validate_memory_operator_id(operator_id)

    async def migrate(arguments: BaseModel) -> JsonValue:
        del arguments
        receipt = await service.migrate_history()
        return {
            "changed": receipt.changed,
            "from_version": receipt.from_version,
            "to_version": receipt.to_version,
            "backup_name": receipt.backup_name,
            "backup_sha256": receipt.backup_sha256,
        }

    async def history(arguments: BaseModel) -> JsonValue:
        parsed = _History.model_validate(arguments)
        page = await service.review_history(
            parsed.entry_id, after_sequence=parsed.after_sequence, limit=parsed.limit
        )
        records: list[JsonValue] = [
            {
                "sequence": row.sequence,
                "operation": row.operation,
                "before": memory_entry_receipt(row.before, content=True) if row.before else None,
                "after": memory_entry_receipt(row.after, content=True),
            }
            for row in page.records
        ]
        return {"records": records, "next_after": page.next_after}

    async def merge(arguments: BaseModel) -> JsonValue:
        parsed = _Merge.model_validate(arguments.model_dump())
        entry = await service.merge_note(
            entry_id=parsed.entry_id,
            kind=MemoryKind(parsed.kind),
            topic=parsed.topic,
            source_run_id=parsed.source_run_id,
            note=parsed.note(),
            paths=tuple(parsed.paths),
            error_type=parsed.error_type,
        )
        return memory_entry_receipt(entry, content=True)

    async def revise(arguments: BaseModel) -> JsonValue:
        parsed = _Revision.model_validate(arguments.model_dump())
        entry = await service.revise_note(
            parsed.entry_id,
            expected_revision=parsed.expected_revision,
            source_run_id=parsed.source_run_id,
            note=parsed.note(),
        )
        return memory_entry_receipt(entry, content=True)

    definitions = memory_note_maintenance_definitions()
    models: tuple[type[BaseModel], ...] = (
        _Input,
        _History,
        _bound(service, _Merge),
        _bound(service, _Revision),
    )
    return tuple(
        _OperatorTool(definition, model, action)
        for definition, model, action in zip(
            definitions,
            models,
            (migrate, history, merge, revise),
            strict=True,
        )
    )

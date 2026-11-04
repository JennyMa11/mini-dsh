"""Operator-only tool bundle. Never add this bundle to the model tool registry."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import JsonValue, ToolDefinition, redact_text
from minidsh.domain.memory import MAX_MEMORY_BYTES, MAX_SOURCE_ID_CHARS, MemoryEntry
from minidsh.runtime.memory import MemoryService
from minidsh.tools.runtime import Tool, schema_for


class _ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")


class _EntryInput(_ReviewInput):
    expected_revision: int = Field(gt=0)


class _ValidateInput(_EntryInput):
    evidence_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")


class _ApproveInput(_EntryInput):
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: str = Field(min_length=1, max_length=128)


class _CommitInput(_EntryInput):
    approval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _ReviseInput(_EntryInput):
    content: str = Field(min_length=1, max_length=MAX_MEMORY_BYTES)


@dataclass(frozen=True, slots=True)
class _Spec:
    name: str
    input_model: type[BaseModel]
    content: bool = False
    evidence_read: bool = False

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            self.name,
            "Explicit operator maintenance outside any active agent run.",
            schema_for(self.input_model),
            side_effects=not self.content,
            permissions=("memory.maintain", "memory.evidence.read")
            if self.evidence_read
            else ("memory.maintain",),
            approval_required=False,
        )


_SPECS = (
    _Spec("review_memory", _ReviewInput, content=True),
    _Spec("validate_memory", _ValidateInput, evidence_read=True),
    _Spec("approve_memory", _ApproveInput, evidence_read=True),
    _Spec("commit_memory", _CommitInput, evidence_read=True),
    _Spec("revise_memory", _ReviseInput),
    _Spec("reject_memory", _EntryInput),
)


def memory_maintenance_definitions() -> tuple[ToolDefinition, ...]:
    """Metadata discovery does not initialize a private store or any plugin."""
    return tuple(spec.definition() for spec in _SPECS)


def validate_memory_operator_id(operator_id: str) -> None:
    if (
        not operator_id.strip()
        or len(operator_id) > MAX_SOURCE_ID_CHARS
        or redact_text(operator_id) != operator_id
    ):
        raise ValueError("memory_operator_invalid")


def _receipt(entry: MemoryEntry, *, content: bool) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "entry_id": entry.entry_id,
        "project_id": entry.project_id,
        "applicable_version": entry.applicable_version,
        "status": entry.status.value,
        "revision": entry.revision,
        "content_sha256": entry.content_sha256,
        "proposal_sha256": entry.proposal_sha256,
        "source_run_id": entry.source_run_id,
        "evidence_sha256": entry.evidence_sha256,
        "approved_by": entry.approved_by,
        "approved_at": entry.approved_at,
        "approved_revision": entry.approved_revision,
        "approval_sha256": entry.approval_sha256,
    }
    if content:
        result.update(
            content=entry.content,
            kind=entry.kind.value,
            topic=entry.topic,
            paths=list(entry.paths),
            error_type=entry.error_type,
        )
    return result


class _MaintenanceTool:
    def __init__(
        self,
        spec: _Spec,
        action: Callable[[BaseModel], Awaitable[MemoryEntry]],
    ) -> None:
        self.input_model = spec.input_model
        self._action = action
        self._content = spec.content
        self.definition = spec.definition()

    async def execute(self, arguments: BaseModel) -> JsonValue:
        return _receipt(await self._action(arguments), content=self._content)


def memory_maintenance_tools(service: MemoryService, *, operator_id: str) -> tuple[Tool, ...]:
    """The trusted application binds the operator; a tool argument cannot impersonate them."""
    validate_memory_operator_id(operator_id)

    async def review(arguments: BaseModel) -> MemoryEntry:
        parsed = _ReviewInput.model_validate(arguments)
        return await service.review(parsed.entry_id)

    async def validate(arguments: BaseModel) -> MemoryEntry:
        parsed = _ValidateInput.model_validate(arguments)
        return await service.validate(
            parsed.entry_id,
            expected_revision=parsed.expected_revision,
            evidence_id=parsed.evidence_id,
        )

    async def approve(arguments: BaseModel) -> MemoryEntry:
        parsed = _ApproveInput.model_validate(arguments)
        return await service.approve(
            parsed.entry_id,
            expected_revision=parsed.expected_revision,
            content_sha256=parsed.content_sha256,
            approver=operator_id,
            approved_at=parsed.approved_at,
        )

    async def commit(arguments: BaseModel) -> MemoryEntry:
        parsed = _CommitInput.model_validate(arguments)
        return await service.commit(
            parsed.entry_id,
            expected_revision=parsed.expected_revision,
            approval_sha256=parsed.approval_sha256,
        )

    async def revise(arguments: BaseModel) -> MemoryEntry:
        parsed = _ReviseInput.model_validate(arguments)
        return await service.revise(
            parsed.entry_id, expected_revision=parsed.expected_revision, content=parsed.content
        )

    async def reject(arguments: BaseModel) -> MemoryEntry:
        parsed = _EntryInput.model_validate(arguments)
        return await service.reject(parsed.entry_id, expected_revision=parsed.expected_revision)

    actions = {
        "review_memory": review,
        "validate_memory": validate,
        "approve_memory": approve,
        "commit_memory": commit,
        "revise_memory": revise,
        "reject_memory": reject,
    }
    return tuple(_MaintenanceTool(spec, actions[spec.name]) for spec in _SPECS)

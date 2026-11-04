"""Explicit project-scoped proposal capability; no automatic writes or registration."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import JsonValue, Message, ModelRequest, Role, ToolDefinition, ToolObservation
from minidsh.domain.extensions import HookView
from minidsh.domain.memory import (
    MAX_MEMORY_BYTES,
    MAX_MEMORY_ERROR_CHARS,
    MAX_MEMORY_PATH_CHARS,
    MAX_MEMORY_PATHS,
    MAX_PROJECT_ENTRIES,
    MemoryEntry,
    MemoryKind,
    MemoryOmission,
    MemorySelection,
    MemoryStatus,
    MemoryValidationError,
    select_memory,
)
from minidsh.domain.memory_content import MemoryContentPolicy
from minidsh.ports.memory import (
    MemoryEvidenceError,
    MemoryEvidenceSource,
    MemoryStore,
    MemoryStoreError,
)
from minidsh.tools.runtime import ToolError, schema_for


class MemoryService:
    """The embedding application supplies scope and brackets the entire run.

    Only explicit tool execution may call propose. Terminal notifications never
    create or promote records. The composition root owns the full run bracket.
    """

    def __init__(  # noqa: PLR0913 - explicit storage, scope, evidence and output policy
        self,
        store: MemoryStore,
        *,
        project_id: str,
        applicable_version: str,
        evidence: MemoryEvidenceSource | None = None,
        forbidden_content_sha256: tuple[str, ...] = (),
        detailed_tool_feedback: bool = True,
    ) -> None:
        self._store = store
        self._project_id = project_id
        self._version = applicable_version
        self._active_run: str | None = None
        self._maintaining = False
        self._evidence = evidence
        self._run_token: object | None = None
        self._snapshot: tuple[MemoryEntry, ...] | None = None
        self._query: tuple[tuple[str, ...], str | None] | None = None
        self._selection: MemorySelection | None = None
        self._base_policy = MemoryContentPolicy(forbidden_content_sha256)
        self._run_policy: MemoryContentPolicy | None = None
        self._entry_policies: dict[str, MemoryContentPolicy] = {}
        self._detailed_feedback = detailed_tool_feedback

    def begin_run(self, run_id: str, *, task: str | None = None) -> None:
        if self._active_run is not None:
            raise ToolError("memory_run_active", "memory_run_active")
        if self._maintaining:
            raise ToolError("memory_maintenance_active", "memory_maintenance_active")
        policy = self._base_policy.copy()
        if task is not None:
            policy.observe((Message(Role.USER, task),))
        self._active_run = run_id
        self._run_token = object()
        self._run_policy = policy

    def end_run(self, run_id: str) -> None:
        if self._active_run != run_id:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        self._active_run = None
        self._run_token = None
        self._snapshot = None
        self._selection = None
        self._query = None
        self._run_policy = None

    def observe_context(self, run_id: str, messages: tuple[Message, ...]) -> None:
        if self._active_run != run_id or self._run_policy is None:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        self._run_policy.observe(messages)

    async def before_model(self, request: ModelRequest, view: HookView) -> ModelRequest:
        self.observe_context(view.run_id, (*view.messages, *request.messages))
        messages: list[Message] = []
        for message in request.messages:
            content = self._model_value(message.content)
            assert isinstance(content, str)
            calls = []
            for call in message.tool_calls:
                arguments = self._model_value(call.arguments)
                assert isinstance(arguments, dict)
                calls.append(replace(call, arguments=arguments))
            reasoning = self._model_value(message.reasoning_content)
            assert reasoning is None or isinstance(reasoning, str)
            safe = replace(
                message, content=content, tool_calls=tuple(calls), reasoning_content=reasoning
            )
            if safe != message and message.role in (Role.SYSTEM, Role.USER):
                raise ToolError("memory_forbidden_content", "memory_forbidden_content")
            messages.append(safe)
        # This is a derived request view. The factual history and call identities
        # remain intact, including the tool response paired with a rejected call.
        return replace(request, messages=tuple(messages))

    def _model_value(self, value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {key: self._model_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._model_value(item) for item in value]
        if not isinstance(value, str):
            return value
        try:
            self._base_policy.check(value)
        except MemoryValidationError as exc:
            if exc.code == "memory_forbidden_content":
                return "[REDACTED]"
            raise ToolError(exc.code, exc.code) from None
        try:
            decoded: JsonValue = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if isinstance(decoded, (dict, list)):
            safe = self._model_value(decoded)
            if safe != decoded:
                value = json.dumps(safe, sort_keys=True)
        return value

    async def after_tool(self, observation: ToolObservation, view: HookView) -> None:
        # Observe before the next call in the same response, without including
        # model-authored arguments or variable wall-clock telemetry as raw output.
        payload: dict[str, JsonValue] = {
            "status": observation.status.value,
            "result": observation.result,
        }
        if observation.summary:
            payload["summary"] = observation.summary
        if self._detailed_feedback:
            payload.update(
                truncated=observation.truncated,
                retryable=observation.retryable,
                artifacts=list(observation.artifacts),
            )
        if observation.error is not None:
            payload["error"] = {
                "code": observation.error.code,
                "message": observation.error.message,
                "details": observation.error.details,
            }
        self.observe_context(
            view.run_id,
            (
                Message(
                    Role.TOOL,
                    json.dumps(payload, sort_keys=True),
                    name=observation.tool_name,
                    tool_call_id=observation.call_id,
                ),
            ),
        )

    def _check_content(self, entry: MemoryEntry, *, content: str | None = None) -> None:
        body = entry.content if content is None else content
        self._base_policy.check(body)
        policy = self._entry_policies.get(entry.entry_id)
        if policy is not None:
            policy.check(body)

    def selection_for(
        self, run_id: str, *, paths: tuple[str, ...] = (), error_type: str | None = None
    ) -> MemorySelection | None:
        query = (tuple(sorted(set(paths))), error_type)
        if self._active_run != run_id or query != self._query:
            return None
        return self._selection

    async def retrieve(
        self, run_id: str, *, paths: tuple[str, ...] = (), error_type: str | None = None
    ) -> MemorySelection:
        if self._active_run != run_id:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        token = self._run_token
        try:
            snapshot = self._snapshot
            if snapshot is None:
                snapshot = await self._store.snapshot(self._project_id)
            selected = select_memory(
                snapshot,
                project_id=self._project_id,
                applicable_version=self._version,
                paths=paths,
                error_type=error_type,
            )
            usable: list[MemoryEntry] = []
            omissions = list(selected.omissions)
            for entry in selected.entries:
                try:
                    await self._fresh_source(entry)
                except (ToolError, MemoryEvidenceError, MemoryValidationError) as exc:
                    omissions.append(MemoryOmission(entry.entry_id, exc.code))
                else:
                    usable.append(entry)
            if self._run_token is not token or self._active_run != run_id:
                raise ToolError("memory_run_mismatch", "memory_run_mismatch")
            self._snapshot = snapshot
            self._query = (tuple(sorted(set(paths))), error_type)
            self._selection = MemorySelection(
                tuple(usable),
                tuple(sorted(omissions, key=lambda item: (item.entry_id, item.reason))),
            )
            return self._selection
        except (MemoryValidationError, MemoryStoreError) as exc:
            raise ToolError(exc.code, exc.code) from None

    @asynccontextmanager
    async def _outside_run(self) -> AsyncIterator[None]:
        if self._active_run is not None:
            raise ToolError("memory_run_active", "memory_run_active")
        if self._maintaining:
            raise ToolError("memory_maintenance_active", "memory_maintenance_active")
        self._maintaining = True
        try:
            yield
        except (MemoryValidationError, MemoryStoreError, MemoryEvidenceError) as exc:
            raise ToolError(exc.code, exc.code) from None
        finally:
            self._maintaining = False

    async def _entry(self, entry_id: str, revision: int | None = None) -> MemoryEntry:
        entry = await self._store.get(self._project_id, entry_id)
        if entry is None:
            raise ToolError("memory_not_found", "memory_not_found")
        if entry.applicable_version != self._version:
            raise ToolError("memory_version_mismatch", "memory_version_mismatch")
        if revision is not None and revision != entry.revision:
            raise ToolError("memory_revision_stale", "memory_revision_stale")
        return entry

    async def review(self, entry_id: str) -> MemoryEntry:
        async with self._outside_run():
            return await self._entry(entry_id)

    async def revise(self, entry_id: str, *, expected_revision: int, content: str) -> MemoryEntry:
        async with self._outside_run():
            entry = await self._entry(entry_id, expected_revision)
            self._check_content(entry, content=content)
            return await self._store.replace(
                entry, entry.revise(content=content, expected_revision=expected_revision)
            )

    async def reject(self, entry_id: str, *, expected_revision: int) -> MemoryEntry:
        async with self._outside_run():
            entry = await self._entry(entry_id, expected_revision)
            rejected = entry.reject(expected_revision=expected_revision)
            return entry if rejected == entry else await self._store.replace(entry, rejected)

    async def validate(
        self, entry_id: str, *, expected_revision: int, evidence_id: str
    ) -> MemoryEntry:
        async with self._outside_run():
            entry = await self._entry(entry_id, expected_revision)
            if entry.status is not MemoryStatus.PROPOSED:
                raise ToolError("memory_transition_invalid", "memory_transition_invalid")
            if self._evidence is None:
                raise ToolError("memory_evidence_unavailable", "memory_evidence_unavailable")
            self._check_content(entry)
            evidence = await self._evidence.resolve(evidence_id)
            validated = entry.validate(evidence)
            await self._no_conflict(validated)
            return await self._store.replace(entry, validated)

    async def approve(
        self,
        entry_id: str,
        *,
        expected_revision: int,
        content_sha256: str,
        approver: str,
        approved_at: str,
    ) -> MemoryEntry:
        async with self._outside_run():
            entry = await self._entry(entry_id, expected_revision)
            approved = entry.approve(
                expected_revision=expected_revision,
                content_sha256=content_sha256,
                approver=approver,
                approved_at=approved_at,
            )
            await self._fresh_source(entry)
            await self._no_conflict(approved)
            return await self._store.replace(entry, approved)

    async def commit(
        self, entry_id: str, *, expected_revision: int, approval_sha256: str
    ) -> MemoryEntry:
        async with self._outside_run():
            entry = await self._entry(entry_id, expected_revision)
            committed = entry.commit(
                expected_revision=expected_revision, approval_sha256=approval_sha256
            )
            if committed == entry:
                return entry
            await self._fresh_source(entry)
            await self._no_conflict(committed)
            return await self._store.replace(entry, committed)

    async def _no_conflict(self, entry: MemoryEntry) -> None:
        others = await self._store.snapshot(self._project_id)
        if any(
            other.entry_id != entry.entry_id
            and other.applicable_version == entry.applicable_version
            and other.topic == entry.topic
            and other.status
            in {MemoryStatus.VALIDATED, MemoryStatus.APPROVED, MemoryStatus.COMMITTED}
            and other.content_sha256 != entry.content_sha256
            for other in others
        ):
            raise ToolError("memory_topic_conflict", "memory_topic_conflict")

    async def _fresh_source(self, entry: MemoryEntry) -> None:
        self._check_content(entry)
        if self._evidence is None or entry.evidence_sha256 is None:
            raise ToolError("memory_evidence_unavailable", "memory_evidence_unavailable")
        evidence = await self._evidence.resolve_sha256(entry.evidence_sha256)
        proposed = replace(
            entry,
            status=MemoryStatus.PROPOSED,
            validation_sha256=None,
            approved_by=None,
            approved_at=None,
            approved_revision=None,
            approval_sha256=None,
        )
        if proposed.validate(evidence).proposal_sha256 != entry.proposal_sha256:
            raise ToolError("memory_evidence_changed", "memory_evidence_changed")

    async def propose(  # noqa: PLR0913 - explicit summary fields, scope is not caller input
        self,
        run_id: str,
        *,
        kind: MemoryKind,
        topic: str,
        content: str,
        paths: tuple[str, ...],
        error_type: str | None,
    ) -> MemoryEntry:
        if self._active_run != run_id:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        try:
            entry = MemoryEntry.propose(
                "memory_" + uuid.uuid4().hex,
                self._project_id,
                kind,
                topic=topic,
                content=content,
                source_run_id=run_id,
                applicable_version=self._version,
                paths=paths,
                error_type=error_type,
            )
            self._base_policy.check(content)
            if self._run_policy is None:
                raise ToolError("memory_run_mismatch", "memory_run_mismatch")
            self._run_policy.check(content)
            if len(self._entry_policies) >= MAX_PROJECT_ENTRIES:
                raise ToolError("memory_source_guard_limit", "memory_source_guard_limit")
            self._entry_policies[entry.entry_id] = self._run_policy.copy()
            return await self._store.create(entry)
        except (MemoryValidationError, MemoryStoreError) as exc:
            raise ToolError(exc.code, exc.code) from None


class _QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    paths: list[Annotated[str, Field(min_length=1, max_length=MAX_MEMORY_PATH_CHARS)]] = Field(
        default_factory=list, max_length=MAX_MEMORY_PATHS
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=MAX_MEMORY_ERROR_CHARS)


class _ProposalInput(_QueryInput):
    kind: Literal["environment", "repair", "diagnosis"]
    topic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    content: str = Field(min_length=1, max_length=MAX_MEMORY_BYTES)


class RetrieveMemoryTool:
    input_model = _QueryInput
    definition = ToolDefinition(
        "retrieve_memory",
        "Prepare a bounded project memory view for Context; return provenance acknowledgements.",
        schema_for(_QueryInput),
        permissions=("memory.read", "memory.evidence.read"),
    )

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        raise ToolError("memory_run_required", "memory_run_required")

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        del step_id
        parsed = _QueryInput.model_validate(arguments)
        selected = await self._service.retrieve(
            run_id, paths=tuple(parsed.paths), error_type=parsed.error_type
        )
        return {
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "revision": entry.revision,
                    "content_sha256": entry.content_sha256,
                    "evidence_sha256": entry.evidence_sha256,
                }
                for entry in selected.entries
            ],
            "omissions": [
                {"entry_id": item.entry_id, "reason": item.reason} for item in selected.omissions
            ],
        }


class ProposeMemoryTool:
    input_model = _ProposalInput
    definition = ToolDefinition(
        "propose_memory",
        "Propose a short project summary. It remains unverified and unavailable as memory.",
        schema_for(_ProposalInput),
        side_effects=True,
        permissions=("memory.propose",),
        approval_required=False,
    )

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        raise ToolError("memory_run_required", "memory_run_required")

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        del step_id
        parsed = _ProposalInput.model_validate(arguments)
        entry = await self._service.propose(
            run_id,
            kind=MemoryKind(parsed.kind),
            topic=parsed.topic,
            content=parsed.content,
            paths=tuple(parsed.paths),
            error_type=parsed.error_type,
        )
        return {
            "entry_id": entry.entry_id,
            "status": entry.status.value,
            "revision": entry.revision,
            "content_sha256": entry.content_sha256,
            "source_run_id": entry.source_run_id,
        }

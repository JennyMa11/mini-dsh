"""Run-owned progressive Memory reads, exposed only through permissioned tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minidsh.domain import (
    Event,
    JsonValue,
    Message,
    ModelRequest,
    Role,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
    estimate_tokens,
    redact_text,
)
from minidsh.domain.memory import (
    MAX_RETRIEVED_ENTRIES,
    MemoryEntry,
    MemoryOmission,
    MemorySelection,
    MemoryValidationError,
    select_memory,
)
from minidsh.domain.memory_content import MemoryContentPolicy
from minidsh.domain.memory_discovery import (
    MAX_QUERY_BYTES,
    SELECTOR_VERSION,
    MemoryReference,
    MemorySearchResult,
    discover_memory,
    snapshot_sha256,
)
from minidsh.domain.memory_query import MemoryMatchState
from minidsh.ports import EventSink
from minidsh.ports.memory import MemoryEvidenceError, MemoryStore, MemoryStoreError
from minidsh.tools.runtime import Tool, ToolError, schema_for

type TokenCounter = Callable[[tuple[Message, ...]], int]


def _encode(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class ProgressiveMemoryReader:
    """Freeze candidates, recheck current authority and publish bounded read views."""

    def __init__(  # noqa: PLR0913 - explicit capabilities and limits
        self,
        store: MemoryStore,
        *,
        project_id: str,
        applicable_version: str,
        validate_source: Callable[[MemoryEntry], Awaitable[None]],
        check_content: Callable[[MemoryEntry], None],
        index_token_limit: int = 512,
        search_token_limit: int = 512,
        read_token_limit: int = 2048,
        count_tokens: TokenCounter = estimate_tokens,
        events: EventSink | None = None,
        forbidden_content_sha256: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._project = project_id
        self._version = applicable_version
        self._validate_source = validate_source
        self._check_content = check_content
        self._index_limit = index_token_limit
        self._search_limit = search_token_limit
        self._read_limit = read_token_limit
        self.count_tokens = count_tokens
        self._events = events
        self._query_policy = MemoryContentPolicy(forbidden_content_sha256)
        self._run: str | None = None
        self._token: object | None = None
        self._snapshot: tuple[MemoryEntry, ...] | None = None
        self._current: dict[str, MemoryEntry] = {}
        self._index: dict[str, JsonValue] | None = None
        self._selection = MemorySelection((), ())
        self._match_state = MemoryMatchState()
        self._observed_state = MemoryMatchState()
        self._search_cards: dict[str, dict[str, JsonValue]] = {}

    def begin_run(self, run_id: str) -> None:
        if self._run is not None:
            raise ToolError("memory_run_active", "memory_run_active")
        self._run, self._token = run_id, object()

    def end_run(self, run_id: str) -> None:
        self._check_run(run_id)
        self._run = self._token = None
        self._snapshot = None
        self._current.clear()
        self._index = None
        self._selection = MemorySelection((), ())
        self._match_state = MemoryMatchState()
        self._observed_state = MemoryMatchState()
        self._search_cards.clear()

    def _check_run(self, run_id: str) -> None:
        if self._run != run_id:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")

    def validate_query_values(self, values: tuple[str, ...]) -> None:
        """Apply registered forbidden-material policy before any storage or call-start event."""
        for value in values:
            self._query_policy.check(value)

    def observe_history(self, history: tuple[Message, ...]) -> None:
        """Only paired, executed tool results establish matching hints."""
        if self._run is None:
            raise ToolError("memory_run_required", "memory_run_required")
        pending: list[ToolCall] = []
        parsed = MemoryMatchState()
        for message in history:
            if message.role is Role.ASSISTANT:
                pending.extend(message.tool_calls)
                continue
            if message.role is not Role.TOOL:
                continue
            index = next(
                (
                    i
                    for i, call in enumerate(pending)
                    if call.call_id == message.tool_call_id and call.name == message.name
                ),
                None,
            )
            if index is None:
                continue
            call = pending.pop(index)
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("status") not in {
                "succeeded",
                "failed",
                "timed_out",
            }:
                continue
            parsed = parsed.observe(call, payload)
        self._match_state = parsed.merge_observed(self._observed_state)

    def observe(self, observation: ToolObservation) -> None:
        if self._run is None:
            raise ToolError("memory_run_required", "memory_run_required")
        call = observation.effective_call
        if call is None or observation.status in {ToolStatus.DENIED, ToolStatus.INVALID}:
            return
        payload: dict[str, JsonValue] = {
            "status": observation.status.value,
            "result": observation.result,
            "error": {"code": observation.error.code} if observation.error else None,
        }
        self._observed_state = self._observed_state.observe(call, payload)
        self._match_state = self._match_state.observe(call, payload)

    def index_for(self, run_id: str) -> dict[str, JsonValue] | None:
        if self._run != run_id:
            return None
        return json.loads(_encode(self._index)) if self._index is not None else None

    def selection_for(self, run_id: str) -> MemorySelection:
        self._check_run(run_id)
        return self._selection

    def messages_for(self, run_id: str) -> tuple[Message, ...]:
        self._check_run(run_id)
        if self._index is None:
            raise ToolError("memory_index_not_prepared", "memory_index_not_prepared")
        messages = [Message(Role.USER, _encode(self._index), name="project_memory_index")]
        if self._selection.entries:
            body = self._fit(
                self._entry_payload(self._selection.entries), "entries", self._read_limit
            )
            messages.append(Message(Role.USER, _encode(body), name="project_memory"))
        return tuple(messages)

    async def _refresh(self, run_id: str) -> None:
        self._check_run(run_id)
        token = self._token
        try:
            current = await self._store.snapshot(self._project)
        except MemoryStoreError as exc:
            raise ToolError(exc.code, exc.code) from None
        self._check_run(run_id)
        if self._token is not token:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        if self._snapshot is None:
            self._snapshot = current
        self._current = {entry.entry_id: entry for entry in current}

    async def _fresh(self, entry: MemoryEntry) -> str | None:
        if self._current.get(entry.entry_id) != entry:
            return "memory_not_available"
        token = self._token
        try:
            self._check_content(entry)
            await self._validate_source(entry)
        except (MemoryEvidenceError, MemoryValidationError, ToolError) as exc:
            return exc.code
        if self._token is not token or self._run is None:
            raise ToolError("memory_run_mismatch", "memory_run_mismatch")
        return None

    def _applicable(self, entry: MemoryEntry) -> bool:
        paths, error = self._match_state.query()
        return bool(
            select_memory(
                (entry,),
                project_id=self._project,
                applicable_version=self._version,
                paths=paths,
                error_type=error,
            ).entries
        )

    def _fit(self, payload: dict[str, JsonValue], key: str, limit: int) -> dict[str, JsonValue]:
        rows = payload[key]
        assert isinstance(rows, list)
        payload["estimated_tokens"] = limit
        dropped = 0
        while self.count_tokens((Message(Role.TOOL, _encode(payload)),)) > limit:
            if not rows:
                raise ToolError("memory_output_budget_exhausted", "memory_output_budget_exhausted")
            rows.pop()
            dropped += 1
            payload["truncated"] = True
            payload["token_omission_count"] = dropped
            payload["omission_reason"] = "token_limit"
        for _ in range(3):
            payload["estimated_tokens"] = self.count_tokens((Message(Role.TOOL, _encode(payload)),))
        return payload

    async def search(  # noqa: PLR0913 - bounded discovery inputs
        self,
        run_id: str,
        *,
        query: str | None = None,
        paths: tuple[str, ...] = (),
        error_type: str | None = None,
        limit: int = 5,
        prepare: bool = False,
    ) -> dict[str, JsonValue]:
        try:
            self.validate_query_values((query or "", *paths, error_type or ""))
            discover_memory(
                (),
                project_id=self._project,
                applicable_version=self._version,
                query=query,
                paths=paths,
                error_type=error_type,
                limit=limit,
            )
        except MemoryValidationError as exc:
            raise ToolError(exc.code, exc.code) from None
        await self._refresh(run_id)
        assert self._snapshot is not None
        try:
            found = discover_memory(
                self._snapshot,
                project_id=self._project,
                applicable_version=self._version,
                query=query,
                paths=paths,
                error_type=error_type,
                limit=limit,
            )
        except MemoryValidationError as exc:
            raise ToolError(exc.code, exc.code) from None
        payload = await self._cards(found)
        payload = self._fit(payload, "cards", self._index_limit if prepare else self._search_limit)
        if prepare:
            entries = {entry.entry_id: entry for entry in self._snapshot}
            for entry_id in tuple(self._search_cards):
                entry = entries.get(entry_id)
                if entry is None or await self._fresh(entry) is not None:
                    self._search_cards.pop(entry_id)
            await self._refresh_selection()
            self._index = payload
        else:
            rows = payload["cards"]
            assert isinstance(rows, list)
            self._search_cards = {
                str(row["entry_id"]): dict(row) for row in rows if isinstance(row, dict)
            }
        return payload

    async def _cards(self, found: MemorySearchResult) -> dict[str, JsonValue]:
        entries = {entry.entry_id: entry for entry in self._snapshot or ()}
        cards: list[JsonValue] = []
        omissions = list(found.omissions)
        for card in found.cards:
            reason = await self._fresh(entries[card.entry_id])
            if reason is not None:
                omissions.append(MemoryOmission(card.entry_id, reason))
            else:
                cards.append(card.as_json())
        return {
            "cards": cards,
            "omissions": [
                {"entry_id": item.entry_id, "reason": item.reason} for item in omissions[:5]
            ],
            "remaining_omission_count": max(0, len(omissions) - 5),
            "snapshot_sha256": found.snapshot_sha256,
            "query_sha256": found.query_sha256,
            "selector_version": found.selector_version,
            "truncated": found.truncated,
        }

    async def _refresh_selection(self) -> None:
        keep: list[MemoryEntry] = []
        omissions: list[MemoryOmission] = []
        for entry in self._selection.entries:
            reason = await self._fresh(entry)
            if reason is None and not self._applicable(entry):
                reason = "memory_not_applicable"
            if reason:
                omissions.append(MemoryOmission(entry.entry_id, reason))
            else:
                keep.append(entry)
        self._selection = MemorySelection(tuple(keep), tuple(omissions))

    async def read(
        self, run_id: str, references: tuple[MemoryReference, ...]
    ) -> dict[str, JsonValue]:
        if not 1 <= len(references) <= MAX_RETRIEVED_ENTRIES or len(
            {item.entry_id for item in references}
        ) != len(references):
            raise ToolError("memory_reference_invalid", "memory_reference_invalid")
        await self._refresh(run_id)
        entries = {entry.entry_id: entry for entry in self._snapshot or ()}
        accepted: list[MemoryEntry] = []
        omissions: list[MemoryOmission] = []
        for reference in references:
            entry = entries.get(reference.entry_id)
            reason: str | None = "memory_not_available"
            if entry is not None:
                eligible = discover_memory(
                    tuple(item for item in entries.values() if item.topic == entry.topic),
                    project_id=self._project,
                    applicable_version=self._version,
                )
                if any(card.entry_id == entry.entry_id for card in eligible.cards):
                    reason = await self._fresh(entry)
                    if reason is None and (
                        entry.revision != reference.expected_revision
                        or entry.content_sha256 != reference.expected_content_sha256
                    ):
                        reason = "memory_reference_stale"
                    if reason is None and not self._applicable(entry):
                        reason = "memory_not_applicable"
            if reason is not None:
                omissions.append(MemoryOmission(reference.entry_id, reason))
            elif entry is not None:
                accepted.append(entry)
        payload = self._entry_payload(tuple(accepted))
        payload["omissions"] = [
            {"entry_id": item.entry_id, "reason": item.reason} for item in omissions
        ]
        payload = self._fit(payload, "entries", self._read_limit)
        rows = payload["entries"]
        assert isinstance(rows, list)
        self._selection = MemorySelection(tuple(accepted[: len(rows)]), tuple(omissions))
        return payload

    @staticmethod
    def _entry_payload(entries: tuple[MemoryEntry, ...]) -> dict[str, JsonValue]:
        return {
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "content": entry.content,
                    "revision": entry.revision,
                    "content_sha256": entry.content_sha256,
                    "source_run_id": entry.source_run_id,
                    "evidence_sha256": entry.evidence_sha256,
                    "approval_sha256": entry.approval_sha256,
                    "applicable_version": entry.applicable_version,
                }
                for entry in entries
            ],
            "omissions": [],
            "truncated": False,
        }

    def sanitize_request(self, request: ModelRequest) -> ModelRequest:
        """Withdraw stale Memory projections, preserving factual call/result identities."""
        calls = {
            call.call_id: call.name for message in request.messages for call in message.tool_calls
        }
        messages: list[Message] = []
        for message in request.messages:
            safe = message
            tool = message.name or calls.get(message.tool_call_id or "")
            if message.role is Role.TOOL and tool in {"read_memory", "search_memory"}:
                key = "entries" if tool == "read_memory" else "cards"
                try:
                    payload = json.loads(message.content)
                    result = payload.get("result", {})
                    rows = result.get(key)
                    if not isinstance(rows, list) or not all(
                        self._matches_projection(row, tool) for row in rows
                    ):
                        safe = replace(message, content='{"status":"memory_not_available"}')
                except (ValueError, TypeError, KeyError, AttributeError):
                    safe = replace(message, content='{"status":"memory_not_available"}')
            messages.append(safe)
        return replace(request, messages=tuple(messages))

    def _matches_projection(self, row: JsonValue, tool: str) -> bool:
        if not isinstance(row, dict) or type(row.get("revision")) is not int:
            return False
        entry_id = row.get("entry_id")
        if not isinstance(entry_id, str):
            return False
        if tool == "read_memory":
            entry = next((e for e in self._selection.entries if e.entry_id == entry_id), None)
            if entry is None:
                return False
            expected = self._entry_payload((entry,))["entries"]
            assert isinstance(expected, list)
            return row == expected[0]
        index_rows = self._index.get("cards", []) if self._index else []
        available = (
            {str(item["entry_id"]): item for item in index_rows if isinstance(item, dict)}
            if isinstance(index_rows, list)
            else {}
        )
        available.update(self._search_cards)
        card = available.get(entry_id)
        if card is None or set(row) != set(card) or type(row.get("score")) is not int:
            return False
        fields = row.get("matched_fields")
        return (
            isinstance(fields, list)
            and all(value in ("topic", "paths", "error_type", "content") for value in fields)
            and all(
                row[key] == value
                for key, value in card.items()
                if key not in {"score", "matched_fields"}
            )
        )

    async def publish_observation(
        self, observation: ToolObservation, *, run_id: str, step_id: int
    ) -> None:
        names = {
            "prepare_memory_index": "memory_index_prepared",
            "search_memory": "memory_search_completed",
            "read_memory": "memory_read_completed",
        }
        if (
            self._events is not None
            and observation.tool_name in names
            and observation.status is ToolStatus.SUCCEEDED
        ):
            payload = observation.result if isinstance(observation.result, dict) else {}
            rows = payload.get("cards", payload.get("entries", []))
            references: list[JsonValue] = []
            if isinstance(rows, list):
                references = [
                    {
                        key: row[key]
                        for key in (
                            "entry_id",
                            "revision",
                            "content_sha256",
                            "evidence_sha256",
                            "approval_sha256",
                            "matched_fields",
                            "score",
                        )
                        if key in row
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
            await self._events.publish(
                Event(
                    names[observation.tool_name],
                    run_id,
                    step_id,
                    {
                        "call_id": observation.call_id,
                        "selector_version": SELECTOR_VERSION,
                        "snapshot_sha256": snapshot_sha256(self._snapshot or ()),
                        "references": references,
                        "status": observation.status.value,
                        "query_sha256": payload.get("query_sha256"),
                        "omissions": payload.get("omissions", []),
                        "remaining_omission_count": payload.get("remaining_omission_count", 0),
                        "token_omission_count": payload.get("token_omission_count", 0),
                        "omission_reason": payload.get("omission_reason"),
                        "estimated_tokens": payload.get("estimated_tokens"),
                        "truncated": observation.truncated or payload.get("truncated", False),
                        "tool_duration_ms": observation.duration_ms,
                    },
                )
            )


class _IndexInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class _SearchInput(_IndexInput):
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=5, ge=1, le=5)
    paths: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=32
    )
    error_type: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("query")
    @classmethod
    def safe_query(cls, value: str) -> str:
        if (
            not value.strip()
            or len(value.encode("utf-8")) > MAX_QUERY_BYTES
            or redact_text(value) != value
        ):
            raise ValueError("memory_query_invalid")
        return value

    @field_validator("paths")
    @classmethod
    def canonical_paths(cls, value: list[str]) -> list[str]:
        select_memory((), project_id="validation", applicable_version="v1", paths=tuple(value))
        if any(redact_text(path) != path for path in value):
            raise ValueError("memory_sensitive_content")
        return value

    @field_validator("error_type")
    @classmethod
    def safe_error(cls, value: str | None) -> str | None:
        if value is not None and redact_text(value) != value:
            raise ValueError("memory_sensitive_content")
        return value


class _ReferenceInput(_IndexInput):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    expected_revision: int = Field(ge=1)
    expected_content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _ReadInput(_IndexInput):
    references: list[_ReferenceInput] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def distinct_ids(self) -> _ReadInput:
        if len({reference.entry_id for reference in self.references}) != len(self.references):
            raise ValueError("memory_reference_duplicate")
        return self


type MemoryReadToolName = Literal["prepare_memory_index", "search_memory", "read_memory"]

_READ_MODELS: dict[MemoryReadToolName, type[BaseModel]] = {
    "prepare_memory_index": _IndexInput,
    "search_memory": _SearchInput,
    "read_memory": _ReadInput,
}


def memory_reading_definitions() -> tuple[ToolDefinition, ...]:
    """Metadata without starting storage; first definition is Context-only."""
    descriptions = (
        "Prepare a bounded, body-free project memory index.",
        "Discover approved topics without granting applicability.",
        "Read references when trusted observations meet their conditions.",
    )
    return tuple(
        ToolDefinition(
            name,
            description,
            schema_for(model),
            permissions=("memory.read", "memory.evidence.read"),
            timeout_seconds=5.0,
        )
        for (name, model), description in zip(_READ_MODELS.items(), descriptions, strict=True)
    )


class _MemoryReadTool:
    def __init__(
        self,
        reader: ProgressiveMemoryReader,
        name: MemoryReadToolName,
    ) -> None:
        self._reader = reader
        self._name = name
        self.input_model = _READ_MODELS[name]
        self.definition = next(
            definition for definition in memory_reading_definitions() if definition.name == name
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        raise ToolError("memory_run_required", "memory_run_required")

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        if self._name == "prepare_memory_index":
            payload = await self._reader.search(run_id, prepare=True)
        elif self._name == "search_memory":
            query = _SearchInput.model_validate(arguments)
            payload = await self._reader.search(
                run_id,
                query=query.query,
                paths=tuple(query.paths),
                error_type=query.error_type,
                limit=query.limit,
            )
        else:
            read = _ReadInput.model_validate(arguments)
            payload = await self._reader.read(
                run_id,
                tuple(
                    MemoryReference(
                        item.entry_id, item.expected_revision, item.expected_content_sha256
                    )
                    for item in read.references
                ),
            )
        del step_id
        return payload


def memory_reading_tools(reader: ProgressiveMemoryReader) -> tuple[Tool, ...]:
    """First tool belongs to Context only; remaining tools may be registered for the model."""
    return (
        _MemoryReadTool(reader, "prepare_memory_index"),
        _MemoryReadTool(reader, "search_memory"),
        _MemoryReadTool(reader, "read_memory"),
    )

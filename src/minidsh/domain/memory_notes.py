"""Approved note structure and lineage; no storage, authority or evidence I/O."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from minidsh.domain.memory import (
    MAX_MEMORY_BYTES,
    MemoryEntry,
    MemoryEvidence,
    MemoryStatus,
    MemoryValidationError,
)
from minidsh.domain.models import JsonValue
from minidsh.domain.redaction import redact_text

NOTE_SCHEMA = "1.0"
MAX_NOTE_SOURCES = 5
MAX_NOTE_EXCEPTIONS = 4
_ID = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA = re.compile(r"[a-f0-9]{64}")
_NOTE_FIELDS = frozenset(
    {"memory_note_schema", "trigger", "rule", "verification", "exceptions", "sources", "supersedes"}
)
_SOURCE_FIELDS = frozenset(
    {"entry_id", "revision", "proposal_sha256", "evidence_sha256", "source_run_id"}
)


def _text(value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MemoryValidationError("memory_note_field_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise MemoryValidationError("memory_note_field_invalid") from None
    if size > maximum:
        raise MemoryValidationError("memory_note_field_invalid")
    if redact_text(value) != value:
        raise MemoryValidationError("memory_sensitive_content")


@dataclass(frozen=True, slots=True)
class MemoryNoteSource:
    entry_id: str
    revision: int
    proposal_sha256: str
    evidence_sha256: str
    source_run_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entry_id, str)
            or _ID.fullmatch(self.entry_id) is None
            or type(self.revision) is not int
            or self.revision < 1
            or not isinstance(self.proposal_sha256, str)
            or _SHA.fullmatch(self.proposal_sha256) is None
            or not isinstance(self.evidence_sha256, str)
            or _SHA.fullmatch(self.evidence_sha256) is None
        ):
            raise MemoryValidationError("memory_note_source_invalid")
        _text(self.source_run_id, 128)

    @classmethod
    def from_entry(cls, entry: MemoryEntry) -> MemoryNoteSource:
        if entry.status is not MemoryStatus.COMMITTED or entry.evidence_sha256 is None:
            raise MemoryValidationError("memory_note_source_unverified")
        return cls(
            entry.entry_id,
            entry.revision,
            entry.proposal_sha256,
            entry.evidence_sha256,
            entry.source_run_id,
        )


def _source(value: JsonValue) -> MemoryNoteSource:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise MemoryValidationError("memory_note_source_invalid")
    entry_id, revision = value["entry_id"], value["revision"]
    proposal, evidence, run = (
        value["proposal_sha256"],
        value["evidence_sha256"],
        value["source_run_id"],
    )
    if (
        not isinstance(entry_id, str)
        or type(revision) is not int
        or not isinstance(proposal, str)
        or not isinstance(evidence, str)
        or not isinstance(run, str)
    ):
        raise MemoryValidationError("memory_note_source_invalid")
    return MemoryNoteSource(entry_id, revision, proposal, evidence, run)


def _unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryValidationError("memory_note_duplicate_key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class MemoryNote:
    trigger: str
    rule: str
    verification: str
    exceptions: tuple[str, ...] = ()
    sources: tuple[MemoryNoteSource, ...] = ()
    supersedes: tuple[MemoryNoteSource, ...] = ()

    def __post_init__(self) -> None:
        _text(self.trigger, 256)
        _text(self.rule, 1024)
        _text(self.verification, 256)
        if not isinstance(self.exceptions, tuple) or len(self.exceptions) > MAX_NOTE_EXCEPTIONS:
            raise MemoryValidationError("memory_note_field_invalid")
        for exception in self.exceptions:
            _text(exception, 128)
        for references in (self.sources, self.supersedes):
            if (
                not isinstance(references, tuple)
                or len(references) > MAX_NOTE_SOURCES
                or any(not isinstance(ref, MemoryNoteSource) for ref in references)
                or len({ref.entry_id for ref in references}) != len(references)
            ):
                raise MemoryValidationError("memory_note_source_invalid")
        if not set(self.supersedes) <= set(self.sources):
            raise MemoryValidationError("memory_note_source_invalid")
        if len(self.to_content().encode("utf-8")) > MAX_MEMORY_BYTES:
            raise MemoryValidationError("memory_note_limit")

    def to_content(self) -> str:
        """All fields enter the existing content hash and approval fingerprint."""
        return json.dumps(
            {"memory_note_schema": NOTE_SCHEMA, **asdict(self)},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_content(cls, content: str) -> MemoryNote:
        try:
            if len(content.encode("utf-8")) > MAX_MEMORY_BYTES:
                raise MemoryValidationError("memory_note_limit")
            value: JsonValue = json.loads(content, object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise MemoryValidationError("memory_note_invalid") from None
        if not isinstance(value, dict) or set(value) != _NOTE_FIELDS:
            raise MemoryValidationError("memory_note_invalid")
        if value["memory_note_schema"] != NOTE_SCHEMA:
            raise MemoryValidationError("memory_note_schema_unsupported")
        trigger, rule, verification = value["trigger"], value["rule"], value["verification"]
        exceptions, sources, supersedes = value["exceptions"], value["sources"], value["supersedes"]
        if (
            not isinstance(trigger, str)
            or not isinstance(rule, str)
            or not isinstance(verification, str)
            or not isinstance(exceptions, list)
            or not all(isinstance(item, str) for item in exceptions)
            or not isinstance(sources, list)
            or not isinstance(supersedes, list)
        ):
            raise MemoryValidationError("memory_note_invalid")
        return cls(
            trigger,
            rule,
            verification,
            tuple(item for item in exceptions if isinstance(item, str)),
            tuple(_source(item) for item in sources),
            tuple(_source(item) for item in supersedes),
        )


def parse_memory_note(content: str) -> MemoryNote | None:
    """Old text stays byte-identical; only the explicit discriminator selects this format."""
    try:
        value = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or "memory_note_schema" not in value:
        return None
    return MemoryNote.from_content(content)


def validate_note_evidence(entry: MemoryEntry, evidence: MemoryEvidence) -> MemoryEntry:
    """Lineage cannot supply the evidence for the new claim it inspired."""
    note = parse_memory_note(entry.content)
    if note is not None and note.sources:
        old_runs = {source.source_run_id for source in note.sources}
        old_evidence = {source.evidence_sha256 for source in note.sources}
        if (
            entry.source_run_id in old_runs
            or evidence.source_run_id != entry.source_run_id
            or evidence.sha256 in old_evidence
        ):
            raise MemoryValidationError("memory_note_evidence_reused")
    return entry.validate(evidence)


def permits_pending_supersession(candidate: MemoryEntry, other: MemoryEntry) -> bool:
    """A pending replacement may coexist with the exact committed old revision."""
    if candidate.status is MemoryStatus.COMMITTED or other.status is not MemoryStatus.COMMITTED:
        return False
    if (candidate.project_id, candidate.applicable_version) != (
        other.project_id,
        other.applicable_version,
    ):
        return False
    note = parse_memory_note(candidate.content)
    return note is not None and MemoryNoteSource.from_entry(other) in note.supersedes


def check_supersession(changes: tuple[tuple[MemoryEntry, MemoryEntry], ...]) -> None:
    """An approved replacement commits together with its exact declared old revisions."""
    if not 1 < len(changes) <= MAX_NOTE_SOURCES + 1:
        raise MemoryValidationError("memory_replacement_invalid")
    if len({old.entry_id for old, _ in changes}) != len(changes):
        raise MemoryValidationError("memory_replacement_invalid")
    committed: list[MemoryEntry] = []
    old_sources: list[MemoryNoteSource] = []
    scope = (changes[0][0].project_id, changes[0][0].applicable_version)
    for before, after in changes:
        before.check_replacement(after)
        if (before.project_id, before.applicable_version) != scope:
            raise MemoryValidationError("memory_scope_mismatch")
        if before.status is MemoryStatus.APPROVED and after.status is MemoryStatus.COMMITTED:
            committed.append(after)
        elif before.status is MemoryStatus.COMMITTED and after.status is MemoryStatus.SUPERSEDED:
            old_sources.append(MemoryNoteSource.from_entry(before))
        else:
            raise MemoryValidationError("memory_replacement_invalid")
    if len(committed) != 1:
        raise MemoryValidationError("memory_replacement_invalid")
    note = parse_memory_note(committed[0].content)
    if note is None or set(note.supersedes) != set(old_sources):
        raise MemoryValidationError("memory_replacement_invalid")

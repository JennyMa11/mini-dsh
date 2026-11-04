"""Immutable project knowledge and explicit validation/approval transitions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePath, PurePosixPath

from minidsh.domain.redaction import redact_text

MAX_MEMORY_BYTES = 2048
MAX_MEMORY_PATHS = 32
MAX_MEMORY_PATH_CHARS = 512
MAX_MEMORY_ERROR_CHARS = 128
MAX_PROJECT_ENTRIES = 100
MAX_RETRIEVED_ENTRIES = 5
MAX_SOURCE_ID_CHARS = 128
MAX_FORBIDDEN_MEMORY_SOURCES = 4096
_ID = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA = re.compile(r"[a-f0-9]{64}")


class MemoryValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MemoryKind(StrEnum):
    ENVIRONMENT = "environment"
    REPAIR = "repair"
    DIAGNOSIS = "diagnosis"


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    APPROVED = "approved"
    COMMITTED = "committed"
    REJECTED = "rejected"


class MemoryEvidenceKind(StrEnum):
    INDEPENDENT_ACCEPTANCE = "independent_acceptance"
    OPERATOR_CONFIGURATION = "operator_configuration"


@dataclass(frozen=True, slots=True)
class MemoryEvidence:
    sha256: str
    project_id: str
    applicable_version: str
    kind: MemoryEvidenceKind
    source_run_id: str
    accepted: bool = False
    source_exists: bool = False
    confirmed_by: str | None = None
    confirmed_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryEvidenceFile:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or self.path == "."
            or len(self.path) > MAX_MEMORY_PATH_CHARS
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or "\x00" in self.path
            or path.as_posix() != self.path
            or _SHA.fullmatch(self.sha256) is None
        ):
            raise MemoryValidationError("memory_evidence_reference_invalid")


@dataclass(frozen=True, slots=True)
class MemoryEvidenceGrant:
    evidence_id: str
    document: MemoryEvidenceFile

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.evidence_id) is None:
            raise MemoryValidationError("memory_identifier_invalid")


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    project_id: str
    applicable_version: str
    database: str
    storage_root: str
    evidence_root: str | None = None
    evidence_grants: tuple[MemoryEvidenceGrant, ...] = ()
    forbidden_content_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.project_id) is None:
            raise MemoryValidationError("memory_identifier_invalid")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", self.applicable_version) is None:
            raise MemoryValidationError("memory_version_invalid")
        for value in (self.database, self.storage_root, self.evidence_root):
            if value is not None and (
                not value
                or "\x00" in value
                or not PurePath(value).is_absolute()
                or ".." in PurePath(value).parts
            ):
                raise MemoryValidationError("memory_config_path_invalid")
        if self.evidence_grants and self.evidence_root is None:
            raise MemoryValidationError("memory_evidence_root_required")
        if (
            not isinstance(self.evidence_grants, tuple)
            or not isinstance(self.forbidden_content_sha256, tuple)
            or len(self.forbidden_content_sha256) > MAX_FORBIDDEN_MEMORY_SOURCES
            or any(_SHA.fullmatch(value) is None for value in self.forbidden_content_sha256)
        ):
            raise MemoryValidationError("memory_config_invalid")
        if len({grant.evidence_id for grant in self.evidence_grants}) != len(self.evidence_grants):
            raise MemoryValidationError("memory_evidence_duplicate")


def _utc(value: str | None) -> None:
    try:
        parsed = datetime.fromisoformat(value or "")
        if parsed.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise MemoryValidationError("memory_approval_time_invalid") from None


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    entry_id: str
    project_id: str
    kind: MemoryKind
    topic: str
    content: str
    source_run_id: str
    applicable_version: str
    paths: tuple[str, ...] = ()
    error_type: str | None = None
    schema_version: str = "1.0"
    revision: int = 1
    status: MemoryStatus = MemoryStatus.PROPOSED
    evidence_sha256: str | None = None
    validation_sha256: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    approved_revision: int | None = None
    approval_sha256: str | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912 - closed record invariants
        if any(_ID.fullmatch(v) is None for v in (self.entry_id, self.project_id, self.topic)):
            raise MemoryValidationError("memory_identifier_invalid")
        if self.schema_version != "1.0" or not isinstance(self.kind, MemoryKind):
            raise MemoryValidationError("memory_schema_invalid")
        if not isinstance(self.status, MemoryStatus) or self.revision < 1:
            raise MemoryValidationError("memory_state_invalid")
        if not self.source_run_id or len(self.source_run_id) > MAX_SOURCE_ID_CHARS:
            raise MemoryValidationError("memory_source_invalid")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", self.applicable_version) is None:
            raise MemoryValidationError("memory_version_invalid")
        for value in self.paths:
            path = PurePosixPath(value)
            if (
                not value
                or value == "."
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in value
                or "\x00" in value
                or path.as_posix() != value
            ):
                raise MemoryValidationError("memory_path_invalid")
        try:
            size = len(self.content.encode("utf-8"))
        except UnicodeError:
            raise MemoryValidationError("memory_content_invalid") from None
        if not self.content.strip() or size > MAX_MEMORY_BYTES:
            raise MemoryValidationError("memory_content_limit")
        sensitive_fields = (
            self.content,
            self.source_run_id,
            *self.paths,
            self.error_type or "",
            self.approved_by or "",
        )
        if any(redact_text(value) != value for value in sensitive_fields):
            raise MemoryValidationError("memory_sensitive_content")
        if self.evidence_sha256 is not None and _SHA.fullmatch(self.evidence_sha256) is None:
            raise MemoryValidationError("memory_evidence_invalid")
        if self.status in {
            MemoryStatus.PROPOSED,
            MemoryStatus.VALIDATED,
            MemoryStatus.REJECTED,
        } and any(
            value is not None
            for value in (
                self.approved_by,
                self.approved_at,
                self.approved_revision,
                self.approval_sha256,
            )
        ):
            raise MemoryValidationError("memory_state_invalid")
        if (
            self.status in {MemoryStatus.PROPOSED, MemoryStatus.REJECTED}
            and self.validation_sha256 is not None
        ):
            raise MemoryValidationError("memory_state_invalid")
        if self.status in {
            MemoryStatus.VALIDATED,
            MemoryStatus.APPROVED,
            MemoryStatus.COMMITTED,
        } and (self.evidence_sha256 is None or self.validation_sha256 != self.proposal_sha256):
            raise MemoryValidationError("memory_validation_stale")
        if self.status in {MemoryStatus.APPROVED, MemoryStatus.COMMITTED}:
            if (
                not self.approved_by
                or self.approved_revision != self.revision
                or self.approval_sha256 != self.proposal_sha256
            ):
                raise MemoryValidationError("memory_approval_stale")
            _utc(self.approved_at)

    @classmethod
    def propose(  # noqa: PLR0913 - explicit immutable proposal fields
        cls,
        entry_id: str,
        project_id: str,
        kind: MemoryKind,
        *,
        topic: str,
        content: str,
        source_run_id: str,
        applicable_version: str,
        paths: tuple[str, ...] = (),
        error_type: str | None = None,
    ) -> MemoryEntry:
        return cls(
            entry_id,
            project_id,
            kind,
            topic,
            content,
            source_run_id,
            applicable_version,
            tuple(sorted(set(paths))),
            error_type,
        )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()

    @property
    def proposal_sha256(self) -> str:
        value = {
            "entry_id": self.entry_id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "topic": self.topic,
            "content": self.content,
            "source_run_id": self.source_run_id,
            "applicable_version": self.applicable_version,
            "paths": self.paths,
            "error_type": self.error_type,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "evidence_sha256": self.evidence_sha256,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def validate(self, evidence: MemoryEvidence) -> MemoryEntry:
        if self.status is not MemoryStatus.PROPOSED:
            raise MemoryValidationError("memory_transition_invalid")
        if (
            evidence.source_exists is not True
            or evidence.accepted is not True
            or _SHA.fullmatch(evidence.sha256) is None
        ):
            raise MemoryValidationError("memory_evidence_unverified")
        if (
            evidence.project_id != self.project_id
            or evidence.applicable_version != self.applicable_version
        ):
            raise MemoryValidationError("memory_evidence_scope_mismatch")
        if self.kind is MemoryKind.ENVIRONMENT:
            if (
                evidence.kind is not MemoryEvidenceKind.OPERATOR_CONFIGURATION
                or not (evidence.confirmed_by or "").strip()
            ):
                raise MemoryValidationError("memory_configuration_unconfirmed")
            _utc(evidence.confirmed_at)
        elif (
            evidence.kind is not MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE
            or evidence.source_run_id != self.source_run_id
        ):
            raise MemoryValidationError("memory_evidence_scope_mismatch")
        bound = replace(self, evidence_sha256=evidence.sha256)
        return replace(
            bound, status=MemoryStatus.VALIDATED, validation_sha256=bound.proposal_sha256
        )

    def approve(
        self, *, expected_revision: int, content_sha256: str, approver: str, approved_at: str
    ) -> MemoryEntry:
        if self.status is not MemoryStatus.VALIDATED:
            raise MemoryValidationError("memory_transition_invalid")
        if expected_revision != self.revision or content_sha256 != self.content_sha256:
            raise MemoryValidationError("memory_approval_stale")
        if not approver.strip():
            raise MemoryValidationError("memory_approver_invalid")
        return replace(
            self,
            status=MemoryStatus.APPROVED,
            approved_by=approver,
            approved_at=approved_at,
            approved_revision=self.revision,
            approval_sha256=self.proposal_sha256,
        )

    def commit(self, *, expected_revision: int, approval_sha256: str | None) -> MemoryEntry:
        if self.status not in {MemoryStatus.APPROVED, MemoryStatus.COMMITTED}:
            raise MemoryValidationError("memory_transition_invalid")
        if expected_revision != self.revision or approval_sha256 != self.approval_sha256:
            raise MemoryValidationError("memory_approval_stale")
        return replace(self, status=MemoryStatus.COMMITTED)

    def revise(self, *, content: str, expected_revision: int) -> MemoryEntry:
        if expected_revision != self.revision:
            raise MemoryValidationError("memory_revision_stale")
        return replace(
            self,
            content=content,
            revision=self.revision + 1,
            status=MemoryStatus.PROPOSED,
            evidence_sha256=None,
            validation_sha256=None,
            approved_by=None,
            approved_at=None,
            approved_revision=None,
            approval_sha256=None,
        )

    def reject(self, *, expected_revision: int) -> MemoryEntry:
        if expected_revision != self.revision:
            raise MemoryValidationError("memory_revision_stale")
        return replace(
            self,
            status=MemoryStatus.REJECTED,
            evidence_sha256=None,
            validation_sha256=None,
            approved_by=None,
            approved_at=None,
            approved_revision=None,
            approval_sha256=None,
        )

    def check_replacement(self, updated: MemoryEntry) -> None:
        """Verify a persisted transition without granting approval or resolving evidence."""
        if (self.project_id, self.entry_id) != (updated.project_id, updated.entry_id):
            raise MemoryValidationError("memory_scope_mismatch")
        if self == updated and self.status is MemoryStatus.COMMITTED:
            return
        if updated == self.reject(expected_revision=self.revision):
            return
        if updated.revision == self.revision + 1 and updated.status is MemoryStatus.PROPOSED:
            if self.revise(content=updated.content, expected_revision=self.revision) != updated:
                raise MemoryValidationError("memory_transition_invalid")
            return
        following = {
            MemoryStatus.PROPOSED: MemoryStatus.VALIDATED,
            MemoryStatus.VALIDATED: MemoryStatus.APPROVED,
            MemoryStatus.APPROVED: MemoryStatus.COMMITTED,
        }
        if updated.revision != self.revision or following.get(self.status) is not updated.status:
            raise MemoryValidationError("memory_transition_invalid")
        material = (
            replace(self, evidence_sha256=updated.evidence_sha256)
            if self.status is MemoryStatus.PROPOSED
            else self
        )
        if material.proposal_sha256 != updated.proposal_sha256:
            raise MemoryValidationError("memory_transition_invalid")
        if self.status is MemoryStatus.APPROVED and (
            self.approved_by,
            self.approved_at,
            self.approved_revision,
            self.approval_sha256,
        ) != (
            updated.approved_by,
            updated.approved_at,
            updated.approved_revision,
            updated.approval_sha256,
        ):
            raise MemoryValidationError("memory_approval_stale")


@dataclass(frozen=True, slots=True)
class MemoryOmission:
    entry_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class MemorySelection:
    entries: tuple[MemoryEntry, ...]
    omissions: tuple[MemoryOmission, ...]


def _memory_mismatch(
    entry: MemoryEntry,
    project_id: str,
    applicable_version: str,
    paths: tuple[str, ...],
    error_type: str | None,
) -> str | None:
    if entry.project_id != project_id:
        return "project_mismatch"
    if entry.status is not MemoryStatus.COMMITTED:
        return "not_committed"
    if entry.applicable_version != applicable_version:
        return "version_mismatch"
    if entry.paths and not any(
        candidate == scope or candidate.startswith(scope + "/")
        for candidate in paths
        for scope in entry.paths
    ):
        return "path_mismatch"
    if entry.error_type is not None and entry.error_type != error_type:
        return "error_mismatch"
    return None


def select_memory(
    entries: tuple[MemoryEntry, ...],
    *,
    project_id: str,
    applicable_version: str,
    paths: tuple[str, ...] = (),
    error_type: str | None = None,
) -> MemorySelection:
    """Select immutable knowledge without storage access or implicit version inference."""
    if _ID.fullmatch(project_id) is None:
        raise MemoryValidationError("memory_identifier_invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", applicable_version) is None:
        raise MemoryValidationError("memory_version_invalid")
    for value in paths:
        path = PurePosixPath(value)
        if (
            not value
            or value == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or "\x00" in value
            or path.as_posix() != value
        ):
            raise MemoryValidationError("memory_path_invalid")
    selected: list[MemoryEntry] = []
    omissions: list[MemoryOmission] = []
    for entry in sorted(entries, key=lambda item: (item.entry_id, item.project_id)):
        reason = _memory_mismatch(entry, project_id, applicable_version, paths, error_type)
        if reason is not None:
            omissions.append(MemoryOmission(entry.entry_id, reason))
        else:
            selected.append(entry)
    contents: dict[str, set[str]] = {}
    for entry in selected:
        contents.setdefault(entry.topic, set()).add(entry.content_sha256)
    unambiguous: list[MemoryEntry] = []
    seen: set[str] = set()
    for entry in selected:
        if len(contents[entry.topic]) > 1:
            omissions.append(MemoryOmission(entry.entry_id, "topic_conflict"))
        elif entry.topic in seen:
            omissions.append(MemoryOmission(entry.entry_id, "duplicate_topic"))
        else:
            seen.add(entry.topic)
            unambiguous.append(entry)
    omissions.extend(
        MemoryOmission(entry.entry_id, "selection_limit")
        for entry in unambiguous[MAX_RETRIEVED_ENTRIES:]
    )
    return MemorySelection(
        tuple(unambiguous[:MAX_RETRIEVED_ENTRIES]),
        tuple(sorted(omissions, key=lambda item: (item.entry_id, item.reason))),
    )

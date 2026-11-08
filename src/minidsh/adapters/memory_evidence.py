"""Private evidence capabilities, bound to immutable file digests by the operator."""

import asyncio
import hashlib
import os
import stat
from contextlib import ExitStack
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from minidsh.adapters.scoring_evidence import ScoringEvidence
from minidsh.domain import JsonValue
from minidsh.domain.memory import (
    MemoryEvidence,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryEvidenceKind,
    MemoryValidationError,
)
from minidsh.ports.memory import MemoryEvidenceError

MAX_EVIDENCE_FILE_BYTES = 8 * 1024 * 1024


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _File(_Record):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Document(_Record):
    schema_version: Literal["1.0"]
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    applicable_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
    source_run_id: str = Field(min_length=1, max_length=128)


class _Independent(_Document):
    kind: Literal["independent_acceptance"]
    acceptance: _File
    trace: _File
    criterion: _File
    artifacts: list[_File] = Field(min_length=1, max_length=16)


class _Configuration(_Document):
    kind: Literal["operator_configuration"]
    configuration: _File
    confirmed_by: str = Field(min_length=1, max_length=128)
    confirmed_at: str = Field(min_length=1, max_length=128)

    @field_validator("confirmed_by")
    @classmethod
    def identified_operator(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operator_required")
        return value

    @field_validator("confirmed_at")
    @classmethod
    def utc_confirmation(cls, value: str) -> str:
        if datetime.fromisoformat(value).utcoffset() != timedelta(0):
            raise ValueError("utc_required")
        return value


_DOCUMENT: TypeAdapter[_Independent | _Configuration] = TypeAdapter(
    Annotated[_Independent | _Configuration, Field(discriminator="kind")]
)


class _Fact(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    schema_version: Literal["1.0"]
    name: str
    run_id: str
    step_id: int = Field(ge=0)
    data: dict[str, JsonValue]


def _closed_trace(data: bytes, run_id: str) -> bool:
    facts = [_Fact.model_validate_json(line) for line in data.splitlines() if line.strip()]
    selected = [fact for fact in facts if fact.run_id == run_id]
    starts = [i for i, fact in enumerate(selected) if fact.name == "run_started"]
    finishes = [i for i, fact in enumerate(selected) if fact.name == "run_finished"]
    if starts != [0] or len(finishes) != 1 or finishes[0] == 0:
        return False
    final = selected[finishes[0]]
    status = final.data.get("status")
    if not isinstance(status, str) or status not in {
        "finished",
        "failed",
        "cancelled",
        "budget_exhausted",
    }:
        return False
    if any(a.step_id > b.step_id for a, b in pairwise(selected)):
        return False
    return not any(
        fact.name.startswith(("model_call_", "tool_call_")) for fact in selected[finishes[0] + 1 :]
    )


class JsonMemoryEvidenceSource:
    """Only named, hash-pinned grants are readable. Paths never come from a model.

    Independent verdicts remain the responsibility of the trusted external verifier.
    This adapter checks their provenance and backing bytes; it does not grade tasks.
    """

    def __init__(
        self, root: Path, *, workspace: Path, grants: tuple[MemoryEvidenceGrant, ...]
    ) -> None:
        if (
            os.open not in os.supports_dir_fd
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
        ):
            raise MemoryEvidenceError("memory_evidence_reader_unsupported")
        try:
            self._root = root.resolve(strict=True)
            actor = workspace.resolve(strict=True)
            if not self._root.is_dir() or self._root.is_relative_to(actor):
                raise ValueError
            if any(parent.samefile(actor) for parent in (self._root, *self._root.parents)):
                raise ValueError
            info = self._root.stat()
            self._root_identity = (info.st_dev, info.st_ino)
        except (OSError, ValueError, RuntimeError):
            raise MemoryEvidenceError("memory_evidence_denied") from None
        self._grants = {grant.evidence_id: grant for grant in grants}
        if len(self._grants) != len(grants):
            raise MemoryEvidenceError("memory_evidence_duplicate")

    def _read(self, reference: MemoryEvidenceFile) -> bytes:
        with ExitStack() as stack:
            directory = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            root_info = os.fstat(directory)
            if (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise MemoryEvidenceError("memory_evidence_denied")
            parts = PurePosixPath(reference.path).parts
            for part in parts[:-1]:
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                stack.callback(os.close, directory)
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            stack.callback(os.close, descriptor)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_EVIDENCE_FILE_BYTES
            ):
                raise MemoryEvidenceError("memory_evidence_denied")
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, MAX_EVIDENCE_FILE_BYTES + 1 - len(data))):
                data.extend(chunk)
            after = os.fstat(descriptor)
            if (
                len(data) > MAX_EVIDENCE_FILE_BYTES
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or hashlib.sha256(data).hexdigest() != reference.sha256
            ):
                raise MemoryEvidenceError("memory_evidence_changed")
            return bytes(data)

    def _read_file(self, reference: _File) -> bytes:
        return self._read(MemoryEvidenceFile(reference.path, reference.sha256))

    async def resolve(self, evidence_id: str) -> MemoryEvidence:
        grant = self._grants.get(evidence_id)
        if grant is None:
            raise MemoryEvidenceError("memory_evidence_not_granted")
        return await asyncio.to_thread(self._resolve, grant)

    async def resolve_sha256(self, sha256: str) -> MemoryEvidence:
        grant = next(
            (grant for grant in self._grants.values() if grant.document.sha256 == sha256), None
        )
        if grant is None:
            raise MemoryEvidenceError("memory_evidence_not_granted")
        return await self.resolve(grant.evidence_id)

    def _resolve(self, grant: MemoryEvidenceGrant) -> MemoryEvidence:
        try:
            document = _DOCUMENT.validate_json(self._read(grant.document))
            if isinstance(document, _Configuration):
                self._read_file(document.configuration)
                return MemoryEvidence(
                    grant.document.sha256,
                    document.project_id,
                    document.applicable_version,
                    MemoryEvidenceKind.OPERATOR_CONFIGURATION,
                    document.source_run_id,
                    accepted=True,
                    source_exists=True,
                    confirmed_by=document.confirmed_by,
                    confirmed_at=document.confirmed_at,
                )
            accepted = ScoringEvidence.model_validate_json(self._read_file(document.acceptance))
            self._read_file(document.criterion)
            for artifact in document.artifacts:
                self._read_file(artifact)
            if (
                accepted.task_success is not True
                or accepted.run_id != document.source_run_id
                or not (accepted.case_id or "").strip()
                or accepted.source != document.criterion.path
                or not _closed_trace(self._read_file(document.trace), document.source_run_id)
            ):
                raise MemoryEvidenceError("memory_evidence_unverified")
            return MemoryEvidence(
                grant.document.sha256,
                document.project_id,
                document.applicable_version,
                MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
                document.source_run_id,
                accepted=True,
                source_exists=True,
            )
        except (ValidationError, MemoryValidationError):
            raise MemoryEvidenceError("memory_evidence_invalid") from None
        except (OSError, UnicodeError, ValueError):
            raise MemoryEvidenceError("memory_evidence_denied") from None

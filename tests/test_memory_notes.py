"""P3 note contracts: structure is approved content, lineage is not validation."""

import hashlib
import json
from dataclasses import replace

import pytest

from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceKind,
    MemoryKind,
    MemoryStatus,
    MemoryValidationError,
)
from minidsh.domain.memory_notes import (
    MemoryNote,
    MemoryNoteSource,
    parse_memory_note,
    validate_note_evidence,
)


def _source() -> MemoryNoteSource:
    return MemoryNoteSource("old_note", 1, "b" * 64, "a" * 64, "old_run")


def _note() -> MemoryNote:
    return MemoryNote(
        trigger="When a SQLite transaction is cancelled.",
        rule="Wait for readback before releasing ownership.",
        verification="Run the cancellation contract.",
        exceptions=("No transaction has started.",),
        sources=(_source(),),
    )


def _entry(note: MemoryNote) -> MemoryEntry:
    return MemoryEntry.propose(
        "new_note",
        "alpha",
        MemoryKind.ENVIRONMENT,
        topic="cancellation_rule",
        content=note.to_content(),
        source_run_id="note_run",
        applicable_version="v1",
    )


def _evidence() -> MemoryEvidence:
    return MemoryEvidence(
        "c" * 64,
        "alpha",
        "v1",
        MemoryEvidenceKind.OPERATOR_CONFIGURATION,
        "note_run",
        accepted=True,
        source_exists=True,
        confirmed_by="fixture_operator",
        confirmed_at="2026-01-01T00:00:00+00:00",
    )


def _change_text(field: str, value: str) -> MemoryNote:
    note = _note()
    if field == "trigger":
        return replace(note, trigger=value)
    if field == "rule":
        return replace(note, rule=value)
    if field == "verification":
        return replace(note, verification=value)
    raise ValueError("unknown fixture field")


@pytest.mark.parametrize("old", ["Use uv.", '{"rule":"plain old JSON"}', '"plain JSON string"'])
def test_n01_old_text_and_its_existing_approval_are_not_reencoded(old: str) -> None:
    assert parse_memory_note(old) is None
    entry = replace(_entry(_note()), content=old)
    validated = validate_note_evidence(entry, _evidence())
    approved = validated.approve(
        expected_revision=1,
        content_sha256=entry.content_sha256,
        approver="fixture_operator",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    assert approved.content.encode() == old.encode()
    assert approved.content_sha256 == hashlib.sha256(old.encode()).hexdigest()
    assert approved.approval_sha256 == validated.proposal_sha256


def test_n01_note_roundtrip_keeps_every_field_and_ignores_json_key_order() -> None:
    note = _note()
    raw = note.to_content()
    parsed = json.loads(raw)
    assert parsed["memory_note_schema"] == "1.0"
    assert parsed["verification"] == "Run the cancellation contract."
    assert parsed["sources"] == [
        {
            "entry_id": "old_note",
            "revision": 1,
            "proposal_sha256": "b" * 64,
            "evidence_sha256": "a" * 64,
            "source_run_id": "old_run",
        }
    ]
    assert parse_memory_note(raw) == note
    reordered = json.dumps(dict(reversed(list(parsed.items()))), indent=1)
    assert MemoryNote.from_content(reordered).to_content() == raw


@pytest.mark.parametrize("change", ["version", "extra", "accepted", "duplicate_key"])
def test_n01_unknown_schema_authority_fields_and_duplicate_keys_are_rejected(change: str) -> None:
    parsed = json.loads(_note().to_content())
    if change == "version":
        parsed["memory_note_schema"] = "9.0"
    elif change == "extra":
        parsed["approved"] = True
    elif change == "accepted":
        parsed["sources"][0]["accepted"] = True
    raw = json.dumps(parsed)
    if change == "duplicate_key":
        raw = raw.replace(
            '"memory_note_schema": "1.0"', '"memory_note_schema":"1.0","memory_note_schema":"9.0"'
        )
    with pytest.raises(MemoryValidationError):
        MemoryNote.from_content(raw)


@pytest.mark.parametrize("revision", [0, -1, True])
def test_n02_lineage_revision_requires_a_strict_positive_integer(revision: int) -> None:
    with pytest.raises(MemoryValidationError):
        replace(_source(), revision=revision)


@pytest.mark.parametrize("field", ["trigger", "rule", "verification"])
def test_n02_note_fields_reject_empty_and_sensitive_text(field: str) -> None:
    with pytest.raises(MemoryValidationError):
        _change_text(field, " ")
    with pytest.raises(MemoryValidationError):
        _change_text(field, "password=note_canary_731")


def test_n02_the_entire_envelope_must_fit_without_dropping_relations() -> None:
    with pytest.raises(MemoryValidationError, match="memory_note_limit"):
        replace(
            _note(),
            rule="x" * 1024,
            trigger="y" * 256,
            verification="z" * 256,
            exceptions=("e" * 128,) * 4,
        )
    with pytest.raises(MemoryValidationError):
        replace(_note(), sources=(_source(), _source()))
    with pytest.raises(MemoryValidationError):
        replace(_note(), supersedes=(replace(_source(), entry_id="unlisted"),))
    with pytest.raises(MemoryValidationError):
        replace(_note(), sources=tuple(replace(_source(), entry_id=f"old_{i}") for i in range(6)))


@pytest.mark.parametrize(
    "field", ["trigger", "rule", "verification", "exceptions", "sources", "supersedes"]
)
def test_n03_every_note_field_changes_approval_material_and_old_approval_is_invalid(
    field: str,
) -> None:
    note = _note()
    changes = {
        "trigger": replace(note, trigger="On cancellation."),
        "rule": replace(note, rule="Retain ownership until checked."),
        "verification": replace(note, verification="Execute the readback check."),
        "exceptions": replace(note, exceptions=("Only before begin.",)),
        "sources": replace(note, sources=(replace(_source(), proposal_sha256="d" * 64),)),
        "supersedes": replace(note, supersedes=(_source(),)),
    }
    changed = changes[field]
    original = validate_note_evidence(_entry(note), _evidence())
    approved = original.approve(
        expected_revision=1,
        content_sha256=original.content_sha256,
        approver="fixture_operator",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    revised = approved.revise(
        content=changed.to_content(), expected_revision=1, source_run_id="revision_run"
    )
    approved.check_replacement(revised)
    assert revised.content_sha256 != approved.content_sha256
    assert revised.proposal_sha256 != approved.proposal_sha256
    assert revised.status is MemoryStatus.PROPOSED
    assert revised.evidence_sha256 is None
    assert revised.approval_sha256 is None
    assert revised.source_run_id == "revision_run"
    with pytest.raises(MemoryValidationError):
        revised.commit(expected_revision=1, approval_sha256=approved.approval_sha256)


@pytest.mark.parametrize("reuse", ["hash", "source_run", "target_run"])
def test_n04_old_lineage_cannot_validate_a_new_claim_even_as_environment_confirmation(
    reuse: str,
) -> None:
    entry, evidence = _entry(_note()), _evidence()
    if reuse == "hash":
        evidence = replace(evidence, sha256="a" * 64)
    elif reuse == "source_run":
        evidence = replace(evidence, source_run_id="old_run")
    else:
        entry = replace(entry, source_run_id="old_run")
        evidence = replace(evidence, source_run_id="old_run")
    with pytest.raises(MemoryValidationError, match="memory_note_evidence_reused"):
        validate_note_evidence(entry, evidence)
    accepted = validate_note_evidence(_entry(_note()), _evidence())
    assert accepted.status is MemoryStatus.VALIDATED
    assert accepted.evidence_sha256 == "c" * 64


def test_n04_new_evidence_still_requires_actual_accepted_and_matching_scope() -> None:
    entry, evidence = _entry(_note()), _evidence()
    for invalid in (
        replace(evidence, accepted=False),
        replace(evidence, project_id="beta"),
        replace(evidence, source_exists=False),
        replace(evidence, applicable_version="v0"),
    ):
        with pytest.raises(MemoryValidationError):
            validate_note_evidence(entry, invalid)

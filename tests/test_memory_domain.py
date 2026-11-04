"""Project Memory state contracts; no storage, process or network capability."""

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


def test_a_repair_requires_validation_and_revision_bound_explicit_approval() -> None:
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Check the failing assertion before editing the source.",
        source_run_id="run_one",
        applicable_version="v1",
        paths=("src/value.py",),
        error_type="AssertionError",
    )
    evidence = MemoryEvidence(
        "a" * 64,
        "project_one",
        "v1",
        MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
        source_run_id="run_one",
        accepted=True,
        source_exists=True,
    )
    validated = proposal.validate(evidence)
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    assert proposal.status is MemoryStatus.PROPOSED
    assert proposal.evidence_sha256 is None
    assert validated.status is MemoryStatus.VALIDATED
    assert validated.evidence_sha256 == "a" * 64
    assert approved.status is MemoryStatus.APPROVED
    assert committed.status is MemoryStatus.COMMITTED
    assert committed.approved_by == "fixture_operator"
    assert committed.approved_revision == 1
    assert committed.approval_sha256 == committed.proposal_sha256
    assert (
        committed.commit(expected_revision=1, approval_sha256=approved.approval_sha256) == committed
    )
    with pytest.raises(MemoryValidationError, match="memory_approval_stale"):
        committed.commit(expected_revision=1, approval_sha256="b" * 64)


def test_editing_approved_content_invalidates_all_previous_authority() -> None:
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Original diagnosis.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    validated = proposal.validate(
        MemoryEvidence(
            "a" * 64,
            "project_one",
            "v1",
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            source_run_id="run_one",
            accepted=True,
            source_exists=True,
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    edited = approved.revise(content="A revised diagnosis.", expected_revision=1)
    assert edited.revision == 2
    assert edited.status is MemoryStatus.PROPOSED
    assert edited.evidence_sha256 is None
    assert edited.validation_sha256 is None
    assert edited.approval_sha256 is None
    assert edited.approved_by is None
    assert edited.approved_at is None
    assert edited.approved_revision is None
    with pytest.raises(MemoryValidationError):
        edited.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    with pytest.raises(MemoryValidationError):
        replace(approved, content="Different text with an old approval.")


@pytest.mark.parametrize("validated", [False, True])
def test_unapproved_states_cannot_carry_approval_authority(*, validated: bool) -> None:
    entry = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Read the failing assertion.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    if validated:
        entry = entry.validate(
            MemoryEvidence(
                "a" * 64,
                "project_one",
                "v1",
                MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
                source_run_id="run_one",
                accepted=True,
                source_exists=True,
            )
        )
    with pytest.raises(MemoryValidationError, match="memory_state_invalid"):
        replace(entry, approved_by="forged_operator")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted", False),
        ("accepted", "true"),
        ("accepted", 1),
        ("source_exists", False),
        ("source_exists", "true"),
        ("source_exists", 1),
        ("project_id", "other_project"),
        ("applicable_version", "v2"),
        ("source_run_id", "another_run"),
        ("kind", MemoryEvidenceKind.OPERATOR_CONFIGURATION),
    ],
)
def test_uncertain_or_out_of_scope_evidence_cannot_validate(field: str, value: object) -> None:
    entry = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    evidence = MemoryEvidence(
        "a" * 64,
        "project_one",
        "v1",
        MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
        source_run_id="run_one",
        accepted=True,
        source_exists=True,
    )
    # Deliberately malformed values at the trusted-evidence boundary.
    invalid = replace(evidence, **{field: value})  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError):
        entry.validate(invalid)


@pytest.mark.parametrize("confirmed_by", [None, "", "  ", "fixture_operator"])
def test_environment_validation_requires_an_identified_configuration_confirmation(
    confirmed_by: str | None,
) -> None:
    entry = MemoryEntry.propose(
        "memory_environment",
        "project_one",
        MemoryKind.ENVIRONMENT,
        topic="test_environment",
        content="Use the project-local test runner.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    evidence = MemoryEvidence(
        "b" * 64,
        "project_one",
        "v1",
        MemoryEvidenceKind.OPERATOR_CONFIGURATION,
        source_run_id="confirmed_configuration",
        accepted=True,
        source_exists=True,
        confirmed_by=confirmed_by,
        confirmed_at="2026-10-29T00:00:00+00:00",
    )
    if confirmed_by == "fixture_operator":
        assert entry.validate(evidence).status is MemoryStatus.VALIDATED
    else:
        with pytest.raises(MemoryValidationError, match="memory_configuration_unconfirmed"):
            entry.validate(evidence)


@pytest.mark.parametrize(
    "status",
    [MemoryStatus.PROPOSED, MemoryStatus.VALIDATED, MemoryStatus.APPROVED, MemoryStatus.COMMITTED],
)
def test_manual_rejection_removes_authority_and_requires_fresh_validation_after_edit(
    status: MemoryStatus,
) -> None:
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    validated = proposal.validate(
        MemoryEvidence(
            "a" * 64,
            "project_one",
            "v1",
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            source_run_id="run_one",
            accepted=True,
            source_exists=True,
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    original = {item.status: item for item in [proposal, validated, approved, committed]}[status]
    rejected = original.reject(expected_revision=1)
    original.check_replacement(rejected)
    assert rejected.status is MemoryStatus.REJECTED
    assert rejected.content == original.content
    assert rejected.revision == 1
    assert rejected.evidence_sha256 is None
    assert rejected.validation_sha256 is None
    assert rejected.approved_by is None
    assert rejected.approval_sha256 is None
    with pytest.raises(MemoryValidationError):
        rejected.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    with pytest.raises(MemoryValidationError):
        replace(rejected, approved_by="forged_operator")
    revised = rejected.revise(content="A corrected diagnosis.", expected_revision=1)
    assert revised.revision == 2
    assert revised.status is MemoryStatus.PROPOSED
    with pytest.raises(MemoryValidationError):
        revised.approve(
            expected_revision=2,
            content_sha256=revised.content_sha256,
            approver="fixture_operator",
            approved_at="2026-10-29T00:00:00+00:00",
        )

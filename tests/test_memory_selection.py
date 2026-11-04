"""Deterministic Memory selection through the pure domain boundary."""

from dataclasses import replace

from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceKind,
    MemoryKind,
    select_memory,
)


def _committed(  # noqa: PLR0913 - explicit scenario fields, not an implementation mock
    entry_id: str,
    *,
    project_id: str = "project_one",
    version: str = "v1",
    paths: tuple[str, ...] = ("src/value.py",),
    error_type: str | None = "AssertionError",
    topic: str = "assertion_diagnosis",
    content: str = "Inspect assertion inputs first.",
) -> MemoryEntry:
    proposed = MemoryEntry.propose(
        entry_id,
        project_id,
        MemoryKind.DIAGNOSIS,
        topic=topic,
        content=content,
        source_run_id="run_one",
        applicable_version=version,
        paths=paths,
        error_type=error_type,
    )
    validated = proposed.validate(
        MemoryEvidence(
            "a" * 64,
            project_id,
            version,
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
    return approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)


def test_selection_excludes_out_of_scope_or_unapproved_records_and_keeps_provenance() -> None:
    good = _committed("applicable")
    proposed = good.revise(content="An unverified diagnosis.", expected_revision=1)
    proposed = replace(proposed, entry_id="unapproved")
    rejected = replace(good.reject(expected_revision=1), entry_id="invalidated")
    entries = (
        good,
        _committed("other_project", project_id="project_two"),
        _committed("old_version", version="v0"),
        _committed("other_path", paths=("src/other.py",)),
        _committed("other_error", error_type="TypeError"),
        proposed,
        rejected,
    )
    selection = select_memory(
        entries,
        project_id="project_one",
        applicable_version="v1",
        paths=("src/value.py",),
        error_type="AssertionError",
    )
    assert selection.entries == (good,)
    assert selection.entries[0].source_run_id == "run_one"
    assert selection.entries[0].evidence_sha256 == "a" * 64
    assert selection.entries[0].revision == 1
    assert {(omission.entry_id, omission.reason) for omission in selection.omissions} == {
        ("other_project", "project_mismatch"),
        ("old_version", "version_mismatch"),
        ("other_path", "path_mismatch"),
        ("other_error", "error_mismatch"),
        ("unapproved", "not_committed"),
        ("invalidated", "not_committed"),
    }


def test_selection_has_a_stable_five_entry_cap_independent_of_input_order() -> None:
    entries = tuple(_committed(f"memory_{i}", topic=f"topic_{i}") for i in range(8))
    selected = select_memory(
        entries,
        project_id="project_one",
        applicable_version="v1",
        paths=("src/value.py",),
        error_type="AssertionError",
    )
    assert [entry.entry_id for entry in selected.entries] == [
        "memory_0",
        "memory_1",
        "memory_2",
        "memory_3",
        "memory_4",
    ]
    assert {(item.entry_id, item.reason) for item in selected.omissions} == {
        ("memory_5", "selection_limit"),
        ("memory_6", "selection_limit"),
        ("memory_7", "selection_limit"),
    }
    assert selected == select_memory(
        tuple(reversed(entries)),
        project_id="project_one",
        applicable_version="v1",
        paths=("src/value.py",),
        error_type="AssertionError",
    )


def test_conflicting_topics_are_omitted_and_identical_knowledge_is_deduplicated() -> None:
    entries = (
        _committed("conflict_a", content="Use the local runner."),
        _committed("conflict_b", content="Use the container runner."),
        _committed("duplicate_a", topic="value_diagnosis"),
        _committed("duplicate_b", topic="value_diagnosis"),
        *(_committed(f"memory_{i}", topic=f"topic_{i}") for i in range(5)),
    )
    selected = select_memory(
        entries,
        project_id="project_one",
        applicable_version="v1",
        paths=("src/value.py",),
        error_type="AssertionError",
    )
    assert [entry.entry_id for entry in selected.entries] == [
        "duplicate_a",
        "memory_0",
        "memory_1",
        "memory_2",
        "memory_3",
    ]
    assert {(item.entry_id, item.reason) for item in selected.omissions} == {
        ("conflict_a", "topic_conflict"),
        ("conflict_b", "topic_conflict"),
        ("duplicate_b", "duplicate_topic"),
        ("memory_4", "selection_limit"),
    }

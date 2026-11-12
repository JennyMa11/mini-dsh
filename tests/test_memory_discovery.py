"""Frozen Memory v2 discovery contracts, independent of storage and models."""

from dataclasses import replace

import pytest

from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceKind,
    MemoryKind,
    MemoryValidationError,
)
from minidsh.domain.memory_discovery import discover_memory, lexical_terms


def _entry(  # noqa: PLR0913 - explicit fixture scope and conditions
    entry_id: str,
    topic: str,
    content: str,
    *,
    paths: tuple[str, ...] = (),
    scope: tuple[str, str] = ("alpha", "v1"),
    error_type: str | None = None,
) -> MemoryEntry:
    project, version = scope
    proposed = MemoryEntry.propose(
        entry_id,
        project,
        MemoryKind.DIAGNOSIS,
        topic=topic,
        content=content,
        source_run_id="source_one",
        applicable_version=version,
        paths=paths,
        error_type=error_type,
    )
    validated = proposed.validate(
        MemoryEvidence(
            "a" * 64,
            project,
            version,
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            "source_one",
            accepted=True,
            source_exists=True,
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    return approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("事务取消", {"事务", "务取", "取消"}),
        ("ＳＱＬＩＴＥ", {"sqlite"}),  # noqa: RUF001 - intentional normalization input
        ("sqlite_cancel", {"sqlite", "cancel"}),
        ("取消", {"取消"}),
        ("!?!", set()),
        ("échec λογική", {"échec", "λογική"}),
        ("错", {"错"}),
    ],
)
def test_mv2_06_normalization_has_frozen_multilingual_terms(text: str, expected: set[str]) -> None:
    assert lexical_terms(text) == frozenset(expected)


def test_mv2_01_discovery_shows_scoped_topics_without_leaking_the_body() -> None:
    scoped = _entry(
        "b_sqlite", "sqlite_cancel", "Cancellation requires readback.", paths=("src/db",)
    )
    result = discover_memory((scoped,), project_id="alpha", applicable_version="v1")
    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.entry_id == "b_sqlite"
    assert card.applicability == "conditional"
    assert card.paths == ("src/db",)
    assert scoped.content not in str(result)
    assert "content" not in card.as_json()
    assert card.as_json()["content_sha256"] == scoped.content_sha256


def test_mv2_05_weighted_search_and_snapshot_are_independent_of_input_order() -> None:
    entries = (
        _entry("a", "sqlite", "x"),
        _entry("c", "third", "sqlite"),
        _entry("b", "other", "sqlite"),
        _entry("d", "path_topic", "x", paths=("sqlite",)),
        _entry("e", "error_topic", "x", error_type="SQLite"),
    )
    first = discover_memory(entries, project_id="alpha", applicable_version="v1", query="sqlite")
    second = discover_memory(
        tuple(reversed(entries)), project_id="alpha", applicable_version="v1", query="sqlite"
    )
    assert first == second
    assert first == discover_memory(
        entries[2:] + entries[:2], project_id="alpha", applicable_version="v1", query="sqlite"
    )
    assert [(card.entry_id, card.score) for card in first.cards] == [
        ("a", 8),
        ("d", 4),
        ("e", 4),
        ("b", 1),
        ("c", 1),
    ]
    assert first.cards[0].matched_fields == ("topic",)
    assert first.cards[1].matched_fields == ("paths",)
    assert first.cards[2].matched_fields == ("error_type",)
    assert first.cards[3].matched_fields == ("content",)
    assert (
        first.cards
        == discover_memory(
            entries, project_id="alpha", applicable_version="v1", query="SQLITE sqlite"
        ).cards
    )


@pytest.mark.parametrize("query", ["中断", "!?!"])
def test_mv2_07_search_does_not_fall_back_to_unrelated_notes(query: str) -> None:
    result = discover_memory(
        (_entry("a", "cancel", "cancel"),),
        project_id="alpha",
        applicable_version="v1",
        query=query,
    )
    assert result.cards == ()


def test_mv2_09_unapproved_other_version_and_other_project_are_not_discoverable() -> None:
    good = _entry("a", "sqlite", "sqlite")
    proposed = good.revise(content="Candidate only.", expected_revision=1)
    old = _entry("old", "old_topic", "Old committed rule.", scope=("alpha", "v0"))
    other = _entry("other", "other_topic", "Foreign committed rule.", scope=("beta", "v1"))
    rejected = replace(good.reject(expected_revision=1), entry_id="rejected")
    result = discover_memory(
        (good, proposed, old, other, rejected), project_id="alpha", applicable_version="v1"
    )
    assert [card.entry_id for card in result.cards] == ["a"]
    assert "beta" not in str(result)
    assert all(item.entry_id != "other" for item in result.omissions)


def test_mv2_10_conflicts_are_excluded_before_query_and_scope_filtering() -> None:
    result = discover_memory(
        (
            _entry("a", "runner", "Use local.", paths=("src/a",)),
            _entry("b", "runner", "Use container.", paths=("src/b",)),
            _entry("c", "sqlite", "sqlite"),
            _entry("d", "sqlite", "sqlite"),
        ),
        project_id="alpha",
        applicable_version="v1",
        query="sqlite",
    )
    assert [card.entry_id for card in result.cards] == ["c"]
    assert {(item.entry_id, item.reason) for item in result.omissions} >= {
        ("a", "topic_conflict"),
        ("b", "topic_conflict"),
        ("d", "duplicate_topic"),
    }


def test_mv2_08_explicit_path_filter_only_narrows_discovery() -> None:
    result = discover_memory(
        (
            _entry("a", "sqlite_a", "sqlite", paths=("src/a",)),
            _entry("b", "sqlite_b", "sqlite", paths=("src/b",)),
        ),
        project_id="alpha",
        applicable_version="v1",
        query="sqlite",
        paths=("src/a/file.py",),
    )
    assert [card.entry_id for card in result.cards] == ["a"]


@pytest.mark.parametrize("query", ["", "x" * 257, "\ud800", "password=memory_v2_canary_9c21"])
def test_mv2_14_invalid_or_sensitive_query_is_rejected(query: str) -> None:
    with pytest.raises(MemoryValidationError):
        discover_memory((), project_id="alpha", applicable_version="v1", query=query)


def test_mv2_15_caps_results_without_changing_the_snapshot() -> None:
    entries = tuple(_entry(f"m_{i}", f"topic_{i}", "sqlite") for i in range(8))
    result = discover_memory(entries, project_id="alpha", applicable_version="v1", query="sqlite")
    assert [card.entry_id for card in result.cards] == [f"m_{i}" for i in range(5)]
    assert result.truncated
    assert (
        result.snapshot_sha256
        == discover_memory(
            entries, project_id="alpha", applicable_version="v1", query="topic"
        ).snapshot_sha256
    )

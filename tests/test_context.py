"""Tests for context retention, compaction, and retrieval."""

from __future__ import annotations

from pathlib import Path

from minidsh.context import (
    FullHistoryContext,
    RepositoryRetriever,
    RetrievalContext,
    SlidingWindowContext,
    SummaryCompactionContext,
)
from minidsh.domain import Message, Role


def _history(count: int) -> tuple[Message, ...]:
    return (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        *(Message(Role.ASSISTANT, f"message-{index}") for index in range(count)),
    )


def test_sliding_window_pins_rules_and_task() -> None:
    expected_omitted = 3
    view = SlidingWindowContext(window_size=2).build(_history(5), "task")

    assert [message.content for message in view.messages] == [
        "rules",
        "task",
        "message-3",
        "message-4",
    ]
    assert view.omitted_messages == expected_omitted


def test_summary_compaction_records_omitted_range() -> None:
    expected_omitted = 3
    view = SummaryCompactionContext(recent_messages=2, summary_max_chars=200).build(
        _history(5), "task"
    )

    assert view.omitted_messages == expected_omitted
    assert view.messages[2].name == "context_summary"
    assert "message-0" in view.messages[2].content
    assert view.messages[-1].content == "message-4"


def test_repository_retrieval_augments_base_context(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("def execute_tool():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "unrelated.md").write_text("nothing here", encoding="utf-8")
    strategy = RetrievalContext(FullHistoryContext(), RepositoryRetriever(tmp_path))

    view = strategy.build(_history(0), "fix execute_tool")

    assert view.metadata["retrieved_chunks"] == 1
    assert view.messages[-1].name == "repo_retrieval"
    assert "agent.py" in view.messages[-1].content

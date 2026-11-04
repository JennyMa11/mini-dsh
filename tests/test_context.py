"""Tests for context retention, compaction, and retrieval."""

from __future__ import annotations

from pathlib import Path

from minidsh.context import (
    BudgetContext,
    FullHistoryContext,
    RepositoryRetriever,
    RetrievalContext,
    SlidingWindowContext,
    SummaryCompactionContext,
)
from minidsh.domain import Message, Role, ToolCall


def _history(count: int) -> tuple[Message, ...]:
    return (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        *(Message(Role.ASSISTANT, f"message-{index}") for index in range(count)),
    )


def _assert_tool_protocol_is_intact(messages: tuple[Message, ...]) -> None:
    """Assert the kept messages form a request an OpenAI-compatible provider accepts.

    Two rules, both enforced with an HTTP 400 rather than a helpful message: a
    ``tool`` message must follow an assistant message declaring its ``tool_call_id``,
    and an assistant message declaring tool calls must have every one of them
    answered inside the window.  A strategy that slices history at a raw index
    breaks the first rule and produces a failure that is indistinguishable from a
    provider outage — which is why this is asserted rather than assumed.
    """
    declared: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        if message.tool_calls:
            declared.update(call.call_id for call in message.tool_calls)
        if message.role is Role.TOOL:
            assert message.tool_call_id in declared, (
                f"tool message {message.tool_call_id!r} is kept without the assistant "
                "message that declares it; a provider rejects this with HTTP 400"
            )
            answered.add(str(message.tool_call_id))
    assert declared == answered, (
        f"assistant declared {sorted(declared - answered)} with no kept message answering "
        "it; a provider rejects this with HTTP 400"
    )


def _round_longer_than_a_window() -> tuple[Message, ...]:
    """A history whose newest tool-call round is wider than one window.

    Indices 4-8 are a single assistant turn that opened four parallel tool calls and
    the four results answering them.  A window that starts anywhere in 5-8 keeps a
    ``tool`` message whose declaring assistant message was dropped, so the only way
    to keep the round whole is to grow the window back to index 4.
    """
    calls = tuple(
        ToolCall(f"c{index}", "read_file", {"path": f"{index}.txt"}) for index in range(4)
    )
    return (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        Message(Role.ASSISTANT, "thinking"),
        Message(Role.ASSISTANT, "more thinking"),
        Message(Role.ASSISTANT, "", tool_calls=calls),
        *(Message(Role.TOOL, f"result-{index}", tool_call_id=f"c{index}") for index in range(4)),
        Message(Role.ASSISTANT, "done"),
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


def test_budget_context_pins_the_followup_and_latest_workspace_instructions() -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "original task"),
        Message(Role.SYSTEM, "old instructions", name="workspace_instructions"),
        Message(Role.ASSISTANT, "a" * 500),
        Message(Role.USER, "current task"),
        Message(Role.SYSTEM, "current instructions", name="workspace_instructions"),
        Message(Role.ASSISTANT, "b" * 500),
    )
    view = BudgetContext(input_budget=40).build(history, "current task")
    kept = [message.content for message in view.messages]
    assert "current task" in kept
    assert "current instructions" in kept
    assert "old instructions" not in kept


def test_budget_context_replaces_tests_run_before_the_first_task() -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.SYSTEM, "stale" * 100, name="verification_status"),
        Message(Role.SYSTEM, "latest result", name="verification_status"),
        Message(Role.USER, "explain latest result"),
    )
    view = BudgetContext(input_budget=40).build(history, "explain latest result")
    assert view.messages == (history[0], history[2], history[3])


def test_summary_compaction_records_omitted_range() -> None:
    expected_omitted = 3
    view = SummaryCompactionContext(recent_messages=2, summary_max_chars=200).build(
        _history(5), "task"
    )

    assert view.omitted_messages == expected_omitted
    assert view.messages[2].name == "context_summary"
    assert "message-0" in view.messages[2].content
    assert view.messages[-1].content == "message-4"


def test_sliding_window_grows_back_to_the_start_of_a_tool_round() -> None:
    history = _round_longer_than_a_window()
    view = SlidingWindowContext(window_size=3).build(history, "task")

    _assert_tool_protocol_is_intact(view.messages)
    # The window grew from 3 messages back to the assistant message that opened the
    # round, so the first non-pinned message is that assistant, not a bare result.
    assert view.messages[2].role is Role.ASSISTANT
    assert view.messages[2].tool_calls is not None
    assert view.omitted_messages == 2
    assert view.metadata["window_extended_for_tool_round"] == 3


def test_summary_compaction_grows_back_to_the_start_of_a_tool_round() -> None:
    history = _round_longer_than_a_window()
    view = SummaryCompactionContext(recent_messages=3, summary_max_chars=200).build(history, "task")

    _assert_tool_protocol_is_intact(view.messages)
    # The summary compacts indices 2-3 and stops there; index 4 opens the round.
    assert view.messages[2].name == "context_summary"
    assert view.messages[3].role is Role.ASSISTANT
    assert view.omitted_messages == 2
    assert view.metadata["window_extended_for_tool_round"] == 3


def test_a_retrieval_wrapper_inherits_the_tool_round_repair(tmp_path: Path) -> None:
    strategy = RetrievalContext(
        SummaryCompactionContext(recent_messages=3, summary_max_chars=200),
        RepositoryRetriever(tmp_path),
    )

    view = strategy.build(_round_longer_than_a_window(), "task")

    _assert_tool_protocol_is_intact(view.messages)


def test_a_round_flush_with_the_pinned_prefix_stops_at_the_floor() -> None:
    """The walk back cannot eat the pinned prefix, so the round is kept in full."""
    calls = (
        ToolCall("c0", "read_file", {"path": "a.txt"}),
        ToolCall("c1", "read_file", {"path": "b.txt"}),
    )
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        Message(Role.ASSISTANT, "", tool_calls=calls),
        Message(Role.TOOL, "result-0", tool_call_id="c0"),
        Message(Role.TOOL, "result-1", tool_call_id="c1"),
    )

    view = SlidingWindowContext(window_size=1).build(history, "task")

    _assert_tool_protocol_is_intact(view.messages)
    # Nothing is dropped: growing back to index 2 already recovers the whole history.
    assert view.messages == history
    assert view.omitted_messages == 0
    assert view.metadata["window_extended_for_tool_round"] == 2


def test_repository_retrieval_augments_base_context(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("def execute_tool():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "unrelated.md").write_text("nothing here", encoding="utf-8")
    strategy = RetrievalContext(FullHistoryContext(), RepositoryRetriever(tmp_path))

    view = strategy.build(_history(0), "fix execute_tool")

    assert view.metadata["retrieved_chunks"] == 1
    assert view.messages[-1].name == "repo_retrieval"
    assert "agent.py" in view.messages[-1].content

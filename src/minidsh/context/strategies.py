"""Deterministic model-context views for reproducible experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minidsh.context.retrieval import RetrievedChunk
from minidsh.domain import ContextView, Message, Role, estimate_tokens

_MIN_SUMMARY_CHARS = 100


def _window_start(history: tuple[Message, ...], start: int, floor: int) -> int:
    """Move ``start`` back so a window never begins inside a tool-call round.

    An OpenAI-compatible provider rejects a request whose ``tool`` message has no
    preceding assistant message declaring the matching ``tool_call_id``, so a
    strategy that keeps only the last N messages must not cut a round in half.
    The window therefore grows backwards to the assistant message that opened the
    round — never past ``floor``, which is the pinned prefix.

    This is invisible under a scripted model, which does not validate the request
    it is handed, and surfaces against a real provider as an opaque HTTP 400 that
    looks exactly like a model failure.
    """
    index = start
    while index > floor and history[index].role is Role.TOOL:
        index -= 1
    return index


class FullHistoryContext:
    """Retain every authoritative message."""

    name: str = "full_history"
    version: str = "1.0"

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        del task
        return ContextView(history, self.name, self.version, estimate_tokens(history))


@dataclass(frozen=True, slots=True)
class SlidingWindowContext:
    """Pin system/task messages and keep the most recent remaining messages."""

    window_size: int = 20
    name: str = "sliding_window"
    version: str = "1.0"

    def __post_init__(self) -> None:
        if self.window_size < 1:
            msg = "window_size must be positive"
            raise ValueError(msg)

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        del task
        if len(history) <= self.window_size + 2:
            selected = history
            start = 0
        else:
            pinned = history[:2]
            start = _window_start(history, len(history) - self.window_size, floor=len(pinned))
            selected = (*pinned, *history[start:])
        omitted = len(history) - len(selected)
        return ContextView(
            selected,
            self.name,
            self.version,
            estimate_tokens(selected),
            omitted_messages=omitted,
            metadata={
                "window_size": self.window_size,
                # Recorded so the adjustment is observable rather than a silent
                # widening of the window.  Zero for the common case.
                "window_extended_for_tool_round": len(history) - start - self.window_size,
            },
        )


@dataclass(frozen=True, slots=True)
class SummaryCompactionContext:
    """Compact older messages into a bounded, deterministic summary."""

    recent_messages: int = 12
    summary_max_chars: int = 4_000
    name: str = "summary_compaction"
    version: str = "1.0"

    def __post_init__(self) -> None:
        if self.recent_messages < 1 or self.summary_max_chars < _MIN_SUMMARY_CHARS:
            msg = "recent_messages must be positive and summary_max_chars must be at least 100"
            raise ValueError(msg)

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        del task
        if len(history) <= self.recent_messages + 2:
            return ContextView(history, self.name, self.version, estimate_tokens(history))

        start = _window_start(history, len(history) - self.recent_messages, floor=2)
        omitted = history[2:start]
        fragments: list[str] = []
        remaining = self.summary_max_chars
        for message in omitted:
            normalized = " ".join(message.content.split())
            fragment = f"[{message.role.value}] {normalized}\n"
            if len(fragment) > remaining:
                fragment = fragment[:remaining]
            fragments.append(fragment)
            remaining -= len(fragment)
            if remaining <= 0:
                break
        summary = Message(
            Role.SYSTEM,
            "Compacted earlier trace (derived, not authoritative):\n" + "".join(fragments),
            name="context_summary",
        )
        # Nothing safely compactable means no summary message at all, rather than
        # an empty one the model has to spend a turn reading.
        head = (*history[:2], summary) if omitted else history[:2]
        selected = (*head, *history[start:])
        return ContextView(
            selected,
            self.name,
            self.version,
            estimate_tokens(selected),
            omitted_messages=len(omitted),
            metadata={
                "recent_messages": self.recent_messages,
                "summary_source_messages": len(omitted),
                "window_extended_for_tool_round": len(history) - start - self.recent_messages,
            },
        )


class Retriever(Protocol):
    """Retrieve task-relevant repository chunks."""

    def search(self, query: str, *, limit: int) -> tuple[RetrievedChunk, ...]:
        """Return ranked chunks."""


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Augment any base strategy with repository or symbol retrieval."""

    base: FullHistoryContext | SlidingWindowContext | SummaryCompactionContext
    retriever: Retriever
    limit: int = 5
    name: str = "repo_retrieval"
    version: str = "1.0"

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        base_view = self.base.build(history, task)
        chunks = self.retriever.search(task, limit=self.limit)
        if not chunks:
            return ContextView(
                base_view.messages,
                f"{self.name}+{base_view.strategy}",
                self.version,
                base_view.estimated_tokens,
                base_view.omitted_messages,
                {**base_view.metadata, "retrieved_chunks": 0},
            )
        rendered = "\n\n".join(
            f"--- {chunk.path} (score={chunk.score:.2f}) ---\n{chunk.content}" for chunk in chunks
        )
        retrieval_message = Message(
            Role.SYSTEM,
            "Task-relevant repository context (read-only derived view):\n" + rendered,
            name="repo_retrieval",
        )
        messages = (*base_view.messages, retrieval_message)
        return ContextView(
            messages,
            f"{self.name}+{base_view.strategy}",
            self.version,
            estimate_tokens(messages),
            base_view.omitted_messages,
            {
                **base_view.metadata,
                "retrieved_chunks": len(chunks),
                "retrieved_paths": [chunk.path for chunk in chunks],
            },
        )

"""Deterministic model-context views for reproducible experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minidsh.context.retrieval import RetrievedChunk
from minidsh.domain import ContextView, Message, Role, estimate_tokens

_MIN_SUMMARY_CHARS = 100


@dataclass(frozen=True, slots=True)
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
        else:
            pinned = history[:2]
            selected = (*pinned, *history[-self.window_size :])
        omitted = len(history) - len(selected)
        return ContextView(
            selected,
            self.name,
            self.version,
            estimate_tokens(selected),
            omitted_messages=omitted,
            metadata={"window_size": self.window_size},
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

        omitted = history[2 : -self.recent_messages]
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
        selected = (*history[:2], summary, *history[-self.recent_messages :])
        return ContextView(
            selected,
            self.name,
            self.version,
            estimate_tokens(selected),
            omitted_messages=len(omitted),
            metadata={
                "recent_messages": self.recent_messages,
                "summary_source_messages": len(omitted),
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

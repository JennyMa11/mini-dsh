"""Tests for final run-state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from minidsh.adapters import JsonSessionStore
from minidsh.domain import Message, Role, RunResult, RunStatus, TokenUsage
from minidsh.infrastructure import SandboxViolationError, WorkspaceSandbox


@pytest.mark.asyncio
async def test_session_round_trip(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    result = RunResult(
        "run123",
        RunStatus.FINISHED,
        "done",
        2,
        TokenUsage(10, 5),
        1,
        12.5,
        history=(Message(Role.USER, "task"), Message(Role.ASSISTANT, "done")),
    )

    await store.save(result)

    assert await store.load("run123") == result
    assert await store.load("missing") is None


def test_sensitive_file_read_is_denied(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")

    with pytest.raises(SandboxViolationError, match="sensitive"):
        WorkspaceSandbox(tmp_path).read_text(".env")

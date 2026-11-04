"""Stable session, single-writer and history-only fork contracts."""

from pathlib import Path

import pytest

from minidsh.adapters.models import ScriptedModel
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.domain import ModelResponse


@pytest.mark.asyncio
async def test_session_appends_turns_without_duplicating_history(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("one"), ModelResponse("two")]),
        options=RuntimeOptions(),
    )
    service = runtime.conversations
    assert service is not None
    first = await service.run_turn("first task", run_id="first")
    second = await service.run_turn("follow up", session_id=first.session_id, run_id="second")
    assert second.session_id == first.session_id
    assert len(second.history) == 5
    summaries = await service.list()
    assert summaries[0].turn_count == 2
    assert summaries[0].latest_run_id == "second"
    log = tmp_path / ".minidsh/conversations" / first.session_id / "events.v1.jsonl"
    assert log.read_text().count('"content": "first task"') == 1


@pytest.mark.asyncio
async def test_fork_only_copies_completed_history(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("one"), ModelResponse("two")]),
        options=RuntimeOptions(),
    )
    service = runtime.conversations
    assert service is not None
    first = await service.run_turn("first task", run_id="first")
    await service.run_turn("later task", session_id=first.session_id, run_id="second")
    (tmp_path / "user.txt").write_text("keep me")
    child = await service.fork("first")
    history = await service.history(child)
    assert len(history) == 3
    assert all(message.content != "later task" for message in history)
    assert (tmp_path / "user.txt").read_text() == "keep me"


@pytest.mark.asyncio
async def test_session_rejects_second_writer(tmp_path: Path) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path, model=ScriptedModel([]), options=RuntimeOptions()
    )
    service = runtime.conversations
    assert service is not None
    identifier = await service.repository.create(())
    first = await service.repository.begin(identifier, "one")
    try:
        with pytest.raises(ValueError, match="busy"):
            await service.repository.begin(identifier, "two")
    finally:
        first.close()

"""Contracts for authoritative, append-only model history."""

from pathlib import Path

import pytest

from minidsh.adapters import JsonSessionStore, ScriptedModel
from minidsh.adapters.session_journal import JsonlSessionJournal
from minidsh.application.session_journal import MemorySessionJournal
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import Message, ModelResponse, Role, RunStatus, ToolCall
from minidsh.infrastructure import SandboxViolationError


@pytest.mark.asyncio
async def test_journal_projects_messages_without_exposing_mutable_call_arguments() -> None:
    journal = MemorySessionJournal()
    log = await journal.create("run")
    call = ToolCall("c", "read_file", {"path": "a"})
    await log.append(1, Message(Role.ASSISTANT, "", tool_calls=(call,)))
    call.arguments["path"] = "changed"
    log.messages()[0].tool_calls[0].arguments["path"] = "also changed"
    assert log.messages()[0].tool_calls[0].arguments == {"path": "a"}
    assert log.events()[0].sequence == 1
    with pytest.raises(ValueError, match="already exists"):
        await journal.create("run")


@pytest.mark.asyncio
async def test_persistent_journal_reopens_and_refuses_overwrite(tmp_path: Path) -> None:
    journal = JsonlSessionJournal(tmp_path)
    log = await journal.create("run")
    await log.append(0, Message(Role.USER, "task"))
    await log.append(1, Message(Role.ASSISTANT, "answer", reasoning_content="reasoning"))
    reopened = await JsonlSessionJournal(tmp_path).open("run")
    assert reopened.messages() == log.messages()
    assert [event.sequence for event in reopened.events()] == [1, 2]
    with pytest.raises(ValueError, match="already exists"):
        await journal.create("run")
    with pytest.raises(ValueError, match="run_id"):
        await journal.create("../outside")


@pytest.mark.asyncio
async def test_loop_history_is_the_session_projection_and_context_never_rewrites_it(
    tmp_path: Path,
) -> None:
    journal = MemorySessionJournal()
    (tmp_path / "a").write_text("contents", encoding="utf-8")
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel(
            [
                ModelResponse(tool_calls=(ToolCall("c", "read_file", {"path": "a"}),)),
                ModelResponse("done"),
            ]
        ),
        options=RuntimeOptions(context_strategy="sliding", sliding_window=1),
        components=RuntimeComponents(journal=journal),
    )
    result = await runtime.agent.run("task", run_id="run")
    log = await journal.open("run")
    assert result.status is RunStatus.FINISHED
    assert result.history == log.messages()
    assert [event.name for event in log.events()] == [
        "system_message",
        "user_message",
        "assistant_message",
        "tool_message",
        "assistant_message",
    ]


@pytest.mark.asyncio
async def test_persistent_journal_redacts_before_projection(tmp_path: Path) -> None:
    log = await JsonlSessionJournal(tmp_path).create("run")
    await log.append(0, Message(Role.USER, "api_key=private-value"))
    reopened = await JsonlSessionJournal(tmp_path).open("run")
    assert reopened.messages() == log.messages()
    assert "private-value" not in log.messages()[0].content


@pytest.mark.asyncio
async def test_journal_rejects_corrupt_sequence(tmp_path: Path) -> None:
    log = await JsonlSessionJournal(tmp_path).create("run")
    await log.append(0, Message(Role.USER, "task"))
    path = tmp_path / ".minidsh/session_events/run.jsonl"
    path.write_text(path.read_text().replace('"sequence": 1', '"sequence": 2'))
    with pytest.raises(ValueError, match="sequence"):
        await JsonlSessionJournal(tmp_path).open("run")


@pytest.mark.asyncio
async def test_terminal_history_is_derived_from_facts_when_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("done")]),
        options=RuntimeOptions(),
    )
    result = await runtime.agent.run("task", run_id="run")
    snapshot = tmp_path / ".minidsh/sessions/run.json"
    snapshot.write_text(snapshot.read_text().replace('"content": "task"', '"content": "stale"'))
    loaded = await JsonSessionStore(tmp_path).load("run")
    assert loaded is not None
    assert loaded.history == result.history


@pytest.mark.asyncio
async def test_reopened_log_cannot_append_or_replace_original_facts(tmp_path: Path) -> None:
    original = await JsonlSessionJournal(tmp_path).create("run")
    await original.append(0, Message(Role.USER, "task"))
    reopened = await JsonlSessionJournal(tmp_path).open("run")
    with pytest.raises(ValueError, match="read-only"):
        await reopened.append(1, Message(Role.ASSISTANT, "changed"))


@pytest.mark.asyncio
async def test_memory_and_disk_apply_the_same_credential_policy(tmp_path: Path) -> None:
    memory = await MemorySessionJournal().create("memory")
    disk = await JsonlSessionJournal(tmp_path).create("disk")
    message = Message(Role.USER, "password=private-value")
    await memory.append(0, message)
    await disk.append(0, message)
    assert memory.messages() == disk.messages()
    assert "private-value" not in memory.messages()[0].content


@pytest.mark.asyncio
async def test_partial_tail_is_rejected_without_rewriting_facts(tmp_path: Path) -> None:
    log = await JsonlSessionJournal(tmp_path).create("run")
    await log.append(0, Message(Role.USER, "task"))
    path = tmp_path / ".minidsh/session_events/run.jsonl"
    broken = path.read_text() + '{"sequence":'
    path.write_text(broken)
    with pytest.raises(ValueError, match="incomplete"):
        await JsonlSessionJournal(tmp_path).open("run")
    assert path.read_text() == broken


@pytest.mark.asyncio
async def test_journal_directory_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".minidsh").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SandboxViolationError):
        await JsonlSessionJournal(workspace).create("run")
    assert list(outside.iterdir()) == []

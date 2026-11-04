"""Terminal editing contracts using real key parsing and in-memory I/O."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput

from minidsh.domain import ToolCall
from minidsh.domain.harness import ApprovalChoice, ApprovalRequest
from minidsh.terminal import _COMMANDS, TerminalInput, _status_text, create_prompt_session


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("个人网页\x7f页面\r", "个人网页面"),
        ("你好\x1b[D的\r", "你的好"),
        ("\x1b[200~第一行\n第二行\x1b[201~\r", "第一行\n第二行"),
        ("第一行\x1b\r第二行\r", "第一行\n第二行"),
        ("第一行\\\r第二行\r", "第一行\n第二行"),
        ("/st\t\r", "/status"),
    ],
)
def test_terminal_edits_unicode_and_submits_paste_as_one_task(keys: str, expected: str) -> None:
    with create_pipe_input() as pipe:
        terminal = TerminalInput(create_prompt_session(input_stream=pipe, output=DummyOutput()))
        pipe.send_text(keys)
        assert terminal.read_task() == expected


def test_slash_completion_lists_matching_commands_with_descriptions() -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        completer = session.completer
        assert completer is not None
        completions = list(
            completer.get_completions(Document("/s", cursor_position=2), CompleteEvent())
        )
        assert {completion.text for completion in completions} == {
            "/status",
            "/sessions",
            "/session",
        }
        assert all(completion.display_meta for completion in completions)
        assert len(list(completer.get_completions(Document(), CompleteEvent()))) == len(_COMMANDS)


@pytest.mark.asyncio
async def test_typing_slash_opens_and_filters_the_command_menu() -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        assert session.complete_while_typing is True
        changes = asyncio.Event()

        def completion_changed(_buffer: Buffer) -> None:
            changes.set()

        session.default_buffer.on_completions_changed += completion_changed

        async def wait_for_count(expected: int) -> None:
            async with asyncio.timeout(2):
                while True:
                    changes.clear()
                    state = session.default_buffer.complete_state
                    if state is not None and len(state.completions) == expected:
                        return
                    await changes.wait()

        task = asyncio.create_task(session.prompt_async())
        try:
            pipe.send_text("/")
            await wait_for_count(len(_COMMANDS))
            pipe.send_text("s")
            await wait_for_count(3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_status_bar_stays_on_one_safe_line_and_fits_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "20")
    assert _status_text("model\x1b[31m\nworkspace\twrite") == "model[31m workspac"
    assert _status_text("\x07\x1b") == ""


def test_prompt_uses_the_status_provider_as_bottom_toolbar() -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(
            input_stream=pipe,
            output=DummyOutput(),
            status_provider=lambda: "deepseek · repo · write · finished",
        )
        toolbar = session.bottom_toolbar
        assert callable(toolbar)
        assert toolbar() == [("class:status", "deepseek · repo · write · finished")]


def test_terminal_history_stays_in_memory_and_can_be_edited() -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        terminal = TerminalInput(session)
        pipe.send_text("个人网页\r")
        assert terminal.read_task() == "个人网页"
        pipe.send_text("\x1b[A\x7f站\r")
        assert terminal.read_task() == "个人网站"
        assert session.history.get_strings() == ["个人网页", "个人网站"]


def test_terminal_cancel_discards_the_draft_and_keeps_prompt_usable() -> None:
    with create_pipe_input() as pipe:
        terminal = TerminalInput(create_prompt_session(input_stream=pipe, output=DummyOutput()))
        pipe.send_text("未提交\x03")
        with pytest.raises(KeyboardInterrupt):
            terminal.read_task()
        pipe.send_text("新任务\r")
        assert terminal.read_task() == "新任务"


def test_terminal_eof_exits_on_empty_input() -> None:
    with create_pipe_input() as pipe:
        terminal = TerminalInput(create_prompt_session(input_stream=pipe, output=DummyOutput()))
        pipe.send_text("\x04")
        with pytest.raises(EOFError):
            terminal.read_task()


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("y\r", ApprovalChoice.ALLOW),
        ("n\r", ApprovalChoice.DENY),
        ("a\r", ApprovalChoice.REMEMBER),
        ("\x04", ApprovalChoice.DENY),
    ],
)
def test_approval_restores_chat_prompt_before_the_next_task(
    keys: str, expected: ApprovalChoice
) -> None:
    with create_pipe_input() as pipe, asyncio.Runner() as runner:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        terminal = TerminalInput(session)
        message, hint = session.message, session.rprompt
        request = ApprovalRequest(
            "approval", "run", 1, ToolCall("call", "patch_file", {}), "preview", "target"
        )
        pipe.send_text(keys)
        assert runner.run(terminal.approve(request)) is expected
        assert session.message == message
        assert session.rprompt == hint
        pipe.send_text("继续修改页面\r")
        assert terminal.read_task() == "继续修改页面"


@pytest.mark.asyncio
async def test_cancelled_approval_restores_chat_prompt() -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        terminal = TerminalInput(session)
        message, hint = session.message, session.rprompt
        request = ApprovalRequest(
            "approval", "run", 1, ToolCall("call", "patch_file", {}), "preview", "target"
        )
        rendered = asyncio.Event()
        session.app.before_render += lambda _: rendered.set()
        pending = asyncio.create_task(terminal.approve(request))
        try:
            async with asyncio.timeout(2):
                await rendered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert session.message == message
            assert session.rprompt == hint
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "rows"), [("", 1), ("第一行\n第二行\n第三行", 3), ("网" * 60, 2)]
)
async def test_editor_renders_only_the_required_rows(draft: str, rows: int) -> None:
    with create_pipe_input() as pipe:
        session = create_prompt_session(input_stream=pipe, output=DummyOutput())
        session.default_buffer.text = draft
        screen = Screen()
        with set_app(session.app):
            session.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(0, 0, 80, 24),
                "",
                erase_bg=False,
                z_index=None,
            )
        await session.app.cancel_and_wait_for_background_tasks()
        assert screen.height == rows


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="real PTY acceptance is POSIX-specific")
def test_real_terminal_shows_the_slash_command_menu() -> None:
    code = """
from minidsh.terminal import TerminalInput
TerminalInput().read_task()
"""
    master, slave = os.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={
            **os.environ,
            "TERM": "xterm-256color",
            "COLUMNS": "100",
            "LINES": "30",
        },
    )
    os.close(slave)
    output = bytearray()
    sent = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if b"\x1b[6n" in chunk:
                os.write(master, b"\x1b[1;1R")
            if b"you > " in output and not sent:
                os.write(master, b"/")
                sent = True
            if b"/status" in output and b"show commands" in output:
                break
        assert b"/status" in output, repr(bytes(output))
        assert b"show commands" in output, repr(bytes(output))
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="real PTY acceptance is POSIX-specific")
@pytest.mark.parametrize(
    ("keys", "expected"), [("个人网页\x7f页面\r", "个人网页面"), ("草稿\x03", "cancelled")]
)
def test_real_terminal_preserves_unicode_and_restores_modes(keys: str, expected: str) -> None:
    code = """
import json, termios
from minidsh.terminal import TerminalInput
before = termios.tcgetattr(0)
terminal = TerminalInput()
try:
    task = terminal.read_task()
except KeyboardInterrupt:
    task = 'cancelled'
after = termios.tcgetattr(0)
# Kernel pending-input bookkeeping can change when returning to cooked mode.
before[3] &= ~getattr(termios, 'PENDIN', 0)
after[3] &= ~getattr(termios, 'PENDIN', 0)
print('RESULT=' + json.dumps(dict(task=task, restored=after == before)))
"""
    master, slave = os.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = bytearray()
    sent = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if b"\x1b[6n" in chunk:
                os.write(master, b"\x1b[1;1R")
            if b"you > " in output and not sent:
                os.write(master, keys.encode())
                sent = True
        text = output.decode(errors="replace")
        assert "RESULT=" in text, repr(text)
        result = json.loads(text.split("RESULT=", 1)[1].splitlines()[0])
        assert result == {"task": expected, "restored": True}
        assert process.wait(timeout=2) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)

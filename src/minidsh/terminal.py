"""Terminal input adapter; editing and history never enter the agent runtime."""

from __future__ import annotations

import os
import sys
from typing import override

from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Always
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from minidsh.domain.harness import ApprovalChoice, ApprovalRequest

_COMMANDS = (
    "/help",
    "/status",
    "/tools",
    "/diff",
    "/test",
    "/key",
    "/clear",
    "/resume",
    "/sessions",
    "/session",
    "/fork",
    "/exit",
)


class _CompactInputLayout(HSplit):
    """Render the required draft rows without filling all space below the cursor."""

    def __init__(self, original: HSplit, buffer: Buffer) -> None:
        super().__init__(original.children)
        self._buffer = buffer

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        rows = self.preferred_height(write_position.width, write_position.height).preferred
        completion = self._buffer.complete_state
        if completion is not None:
            rows += min(8, len(completion.completions))
        compact = WritePosition(
            write_position.xpos,
            write_position.ypos,
            write_position.width,
            min(write_position.height, max(1, rows)),
        )
        super().write_to_screen(screen, mouse_handlers, compact, parent_style, erase_bg, z_index)


def create_prompt_session(
    *, input_stream: Input | None = None, output: Output | None = None
) -> PromptSession[str]:
    """Build a Unicode editor with explicit send/newline keys and memory-only history."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.text.endswith("\\") and buffer.cursor_position == len(buffer.text):
            buffer.delete_before_cursor()
            buffer.insert_text("\n")
        else:
            buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("tab")
    def complete_command(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        prefix = buffer.document.text_before_cursor
        matches = [command for command in _COMMANDS if command.startswith(prefix)]
        if prefix.startswith("/") and len(matches) == 1 and prefix == buffer.text:
            buffer.text = matches[0]
            buffer.cursor_position = len(buffer.text)
        else:
            buffer.start_completion(select_first=False)

    session = PromptSession[str](
        message=[("class:prompt", "you > ")],
        multiline=True,
        prompt_continuation="... > ",
        history=InMemoryHistory(),
        enable_history_search=True,
        completer=WordCompleter(list(_COMMANDS)),
        complete_while_typing=False,
        key_bindings=bindings,
        reserve_space_for_menu=0,
        rprompt=[("class:hint", "Enter: send | Alt+Enter: newline")],
        style=Style.from_dict({"prompt": "ansicyan bold", "hint": "ansibrightblack"}),
        input=input_stream,
        output=output,
    )
    # Grow with the draft, rather than filling the terminal with blank input rows.
    for window in session.layout.find_all_windows():
        if (
            isinstance(window.content, BufferControl)
            and window.content.buffer is session.default_buffer
        ):
            window.dont_extend_height = Always()
    container = session.layout.container
    if isinstance(container, HSplit):
        session.layout = Layout(
            _CompactInputLayout(container, session.default_buffer),
            focused_element=session.layout.current_window,
        )
        session.app.layout = session.layout
    return session


class TerminalInput:
    """Use the editor on a real terminal and ordinary line input for pipes/tests."""

    def __init__(self, session: PromptSession[str] | None = None) -> None:
        self._session = session
        if (
            session is None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb"
        ):
            self._session = create_prompt_session()

    @property
    def enhanced(self) -> bool:
        """Whether the current input surface supports cursor editing and safe paste."""
        return self._session is not None

    def read_task(self) -> str:
        """Return one submitted task; interrupts and EOF propagate to the existing UI."""
        if self._session is not None:
            return self._session.prompt().strip()
        parts: list[str] = []
        line = input("you > ")
        while line.endswith("\\"):
            parts.append(line[:-1])
            line = input("... > ")
        parts.append(line)
        return "\n".join(parts).strip()

    async def approve(self, request: ApprovalRequest) -> ApprovalChoice:
        """Use a cancellable editor prompt; redirected input never approves implicitly."""
        if self._session is None:
            return ApprovalChoice.DENY
        message, hint = self._session.message, self._session.rprompt
        try:
            answer = await self._session.prompt_async(
                f"{request.call.name}: allow once [y], deny [n], remember this run [a]? ",
                rprompt=[("class:hint", "Enter: confirm")],
            )
        except EOFError:
            return ApprovalChoice.DENY
        finally:
            # PromptSession retains prompt overrides, including after cancellation.
            self._session.message, self._session.rprompt = message, hint
        return {"y": ApprovalChoice.ALLOW, "a": ApprovalChoice.REMEMBER}.get(
            answer.strip().lower(),
            ApprovalChoice.DENY,
        )

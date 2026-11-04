"""Terminal input adapter; editing and history never enter the agent runtime."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable
from typing import override

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from minidsh.domain.harness import ApprovalChoice, ApprovalRequest

_COMMANDS: dict[str, str] = {
    "/help": "show commands",
    "/status": "show model, workspace, permissions and last run",
    "/tools": "show registered tools and command permissions",
    "/diff": "show the workspace Git diff",
    "/test": "run the configured tests",
    "/budget": "show or change steps, tokens, tools and time limits",
    "/key": "enter or update the provider API key privately",
    "/clear": "start a fresh conversation",
    "/resume": "continue a saved run ID",
    "/sessions": "list saved conversations and runs",
    "/session": "continue a stable conversation ID",
    "/fork": "fork history through an ended turn",
    "/exit": "exit (also Ctrl+D)",
}


class _CommandCompleter(Completer):
    """Complete slash commands from the first draft line, with descriptions."""

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        del complete_event
        prefix = document.text_before_cursor
        if document.text != prefix or (prefix and not prefix.startswith("/")):
            return
        for command, description in _COMMANDS.items():
            if command.startswith(prefix):
                yield Completion(
                    command,
                    start_position=-len(prefix),
                    display=command,
                    display_meta=description,
                )


def _status_text(value: str) -> str:
    """Collapse untrusted text to one safe line that fits the terminal width."""
    flattened = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = " ".join("".join(c for c in flattened if c.isprintable()).split())
    width = shutil.get_terminal_size((80, 24)).columns
    return text[: max(0, width - 2)]


class _CompactInputLayout(HSplit):
    """Render the required draft rows without filling all space below the cursor."""

    def __init__(self, original: HSplit) -> None:
        super().__init__(original.children)

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
        compact = WritePosition(
            write_position.xpos,
            write_position.ypos,
            write_position.width,
            min(write_position.height, max(1, rows)),
        )
        super().write_to_screen(screen, mouse_handlers, compact, parent_style, erase_bg, z_index)


def create_prompt_session(
    *,
    input_stream: Input | None = None,
    output: Output | None = None,
    status_provider: Callable[[], str] | None = None,
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

    def toolbar() -> StyleAndTextTuples:
        if status_provider is None:
            return []
        text = _status_text(status_provider())
        return [("class:status", text)] if text else []

    session = PromptSession[str](
        message=[("class:prompt", "you > ")],
        multiline=True,
        prompt_continuation="... > ",
        history=InMemoryHistory(),
        # prompt_toolkit disables complete_while_typing while history search is on;
        # Up/Down still navigate the in-memory history without prefix search.
        enable_history_search=False,
        completer=_CommandCompleter(),
        complete_while_typing=True,
        key_bindings=bindings,
        reserve_space_for_menu=0,
        rprompt=[("class:hint", "Enter: send | Alt+Enter: newline")],
        bottom_toolbar=toolbar if status_provider is not None else None,
        style=Style.from_dict(
            {
                "prompt": "ansicyan bold",
                "hint": "ansibrightblack",
                "status": "bg:#303030 #d0d0d0",
                "bottom-toolbar": "bg:#303030 #d0d0d0",
            }
        ),
        input=input_stream,
        output=output,
    )
    default_buffer = session.default_buffer

    def buffer_height() -> Dimension:
        state = default_buffer.complete_state
        if state is None:
            return Dimension()
        return Dimension(min=min(8, len(state.completions)))

    # Grow with the draft and the visible completion menu, without reserving blank rows.
    for window in session.layout.find_all_windows():
        if isinstance(window.content, BufferControl) and window.content.buffer is default_buffer:
            window.dont_extend_height = Always()
            window.height = buffer_height
    container = session.layout.container
    if isinstance(container, HSplit):
        session.layout = Layout(
            _CompactInputLayout(container),
            focused_element=session.layout.current_window,
        )
        session.app.layout = session.layout
    return session


class TerminalInput:
    """Use the editor on a real terminal and ordinary line input for pipes/tests."""

    def __init__(
        self,
        session: PromptSession[str] | None = None,
        *,
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        if (
            session is None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb"
        ):
            self._session = create_prompt_session(status_provider=status_provider)

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

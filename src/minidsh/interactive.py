"""Terminal presentation over the existing single-agent harness."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from minidsh import __version__
from minidsh.adapters import JsonlTraceWriter, JsonSessionStore, ScriptedModel
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import Event, Message, RunResult, ToolCall, redact_text, redact_value

_HELP = """/help             Show commands
/status           Show model, workspace, permissions and last run
/diff             Show the workspace Git diff
/clear            Start a fresh conversation (files stay as they are)
/resume RUN_ID    Continue a saved run from this workspace
/exit             Exit (also Ctrl+D)

Ctrl+C cancels the current task or clears the input.
End a line with a backslash to enter a multiline task.
"""


def _terminal_text(value: str) -> str:
    """Keep untrusted model/tool output from issuing terminal control commands."""
    return "".join(c for c in redact_text(value) if c in "\n\t" or c.isprintable())


def _write(value: str) -> None:
    sys.stdout.write(_terminal_text(value) + "\n")
    sys.stdout.flush()


class InteractiveChat:
    """A synchronous prompt over a persistent, cancellable asyncio runtime."""

    def __init__(  # noqa: PLR0913 - explicit presentation dependencies
        self,
        *,
        workspace: Path,
        runtime_factory: Callable[[], LocalRuntime],
        model_label: str,
        options: RuntimeOptions,
        cost_reportable: bool,
        resume: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._factory = runtime_factory
        self._model_label = model_label
        self._options = options
        self._cost_reportable = cost_reportable
        self._resume = resume
        self._runtime: LocalRuntime | None = None
        self._history: tuple[Message, ...] = ()
        self._last_run: str | None = None
        self._sessions = JsonSessionStore(workspace)
        self._runner = asyncio.Runner()

    def run(self) -> int:
        """Read tasks until EOF or /exit; failed turns leave the prompt available."""
        with self._runner:
            return self._prompt()

    def _prompt(self) -> int:
        _write(f"\nMini-dsh {__version__} · DeepSeek Harness\n")
        self._status()
        _write("\nType a task to begin. /help for commands; /exit to quit.\n")
        if self._resume is not None:
            self._restore(self._resume)
        while True:
            try:
                task = self._read_task()
            except EOFError:
                _write("\nGoodbye.")
                return 0
            except KeyboardInterrupt:
                _write("\nInput cleared.")
                continue
            if not task:
                continue
            if task.startswith("/"):
                if not self._command(task):
                    return 0
            else:
                self._turn(task)

    @staticmethod
    def _read_task() -> str:
        parts: list[str] = []
        line = input("you > ")
        while line.endswith("\\"):
            parts.append(line[:-1])
            line = input("... > ")
        parts.append(line)
        return "\n".join(parts).strip()

    def _status(self) -> None:
        permissions = "write" if self._options.allow_write else "read-only"
        if self._options.allow_shell:
            permissions += f", shell ({', '.join(sorted(self._options.allowed_commands))})"
        _write(f"  Model: {self._model_label}\n  Workspace: {self._workspace}")
        _write(f"  Permissions: {permissions}\n  Context: {self._options.context_strategy}")
        _write(f"  Last run: {self._last_run or '(none)'}")

    def _command(self, task: str) -> bool:
        command, _, argument = task.partition(" ")
        argument = argument.strip()
        if command in {"/exit", "/quit"} and not argument:
            _write("Goodbye.")
            return False
        if command == "/resume" and argument:
            self._restore(argument)
        elif argument:
            _write("Unknown command or arguments. Type /help.")
        elif command == "/help":
            _write(_HELP)
        elif command == "/status":
            self._status()
        elif command == "/clear":
            self._history = ()
            self._last_run = None
            _write("Conversation cleared. Workspace files are unchanged.")
        elif command == "/diff":
            self._diff()
        else:
            _write("Unknown command or missing argument. Type /help.")
        return True

    def _restore(self, run_id: str) -> None:
        try:
            result = self._runner.run(self._sessions.load(run_id))
            if result is None:
                _write(f"No saved run: {run_id}")
                return
            self._history = result.history
            self._last_run = result.run_id
            _write(f"Resumed {run_id} ({result.status.value}, {len(result.history)} messages).")
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Cannot resume: {exc}")

    def _diff(self) -> None:
        # A local diff needs no provider credential and still uses Tool Runtime.
        runtime = self._runtime or build_local_runtime(
            workspace=self._workspace,
            model=ScriptedModel([]),
            options=self._options,
        )
        run_id = uuid.uuid4().hex
        trace = self._workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
        unsubscribe = runtime.events.subscribe("*", JsonlTraceWriter(trace))
        try:
            observation = self._runner.run(
                runtime.tools.execute(
                    ToolCall(uuid.uuid4().hex, "git", {"action": "diff", "paths": []}),
                    run_id,
                    0,
                )
            )
            if observation.error:
                _write(f"Diff unavailable: {observation.error.message}")
            elif isinstance(observation.result, dict):
                result = observation.result
                if result.get("exit_code", 0) != 0:
                    _write(f"Diff unavailable: {result.get('stderr', '')}")
                else:
                    _write(str(result.get("stdout") or "No tracked changes."))
                    if observation.truncated:
                        _write("[Diff truncated; full output is in the tool artifact.]")
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
            _write(f"Diff unavailable: {exc}")
        finally:
            unsubscribe()

    def _turn(self, task: str) -> None:
        try:
            if self._runtime is None:
                self._runtime = self._factory()
                self._runtime.events.subscribe("*", self._progress)
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Cannot start: {exc}")
            return
        runtime = self._runtime
        run_id = uuid.uuid4().hex
        trace = self._workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
        unsubscribe = runtime.events.subscribe("*", JsonlTraceWriter(trace))
        try:
            result = self._runner.run(
                runtime.agent.run(
                    task,
                    run_id=run_id,
                    prior_history=self._history,
                )
            )
            self._history = result.history
            self._last_run = result.run_id
            self._show_result(result)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _write("\nTask cancelled. Returning to the prompt.")
            # AgentLoop persists its cancelled state before propagating cancellation.
            self._restore(run_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Task failed: {exc}")
        finally:
            unsubscribe()
            _write(f"  Run: {run_id}\n  Trace: {trace}\n")

    def _show_result(self, result: RunResult) -> None:
        if result.answer:
            _write(f"\nassistant > {result.answer}\n")
        if result.failure_reason:
            _write(f"  {result.status.value}: {result.failure_reason}")
        cost = f"${result.cost_usd:.6f}" if self._cost_reportable else "unavailable (no rate card)"
        _write(
            f"  {result.status.value} · {result.steps} steps · {result.tool_calls} tools · "
            f"{result.token_usage.total_tokens} tokens · {result.duration_ms / 1000:.1f}s · "
            f"cost {cost}"
        )

    async def _progress(self, event: Event) -> None:
        if event.name == "sub_agent_started":
            _write(f"  Sub Agent {event.data.get('name')} ({event.data.get('role')}): started")
        elif event.name == "sub_agent_finished":
            _write(f"  Sub Agent {event.data.get('name')}: {event.data.get('status')}")
        elif event.name == "model_call_started":
            _write(f"  [{event.step_id}] Thinking…")
        elif event.name == "model_call_completed" and event.data.get("tool_call_count"):
            content = event.data.get("content")
            if isinstance(content, str) and content:
                _write(content[:2000])
        elif event.name == "tool_call_started":
            arguments = json.dumps(redact_value(event.data.get("arguments")), ensure_ascii=False)
            _write(f"  → {event.data.get('tool')} {arguments[:240]}")
        elif event.name == "tool_call_completed":
            _write(f"  ← {event.data.get('tool')}: {event.data.get('status')}")
            error = event.data.get("error")
            if isinstance(error, dict):
                _write(f"    {error.get('message', '')}")
        elif event.name == "session_save_failed":
            _write("  Session could not be saved; this turn cannot be resumed from disk.")

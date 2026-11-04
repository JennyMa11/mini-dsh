"""Terminal presentation over the existing single-agent harness."""

from __future__ import annotations

import asyncio
import getpass
import json
import shlex
import sys
import uuid
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from minidsh import __version__
from minidsh.adapters import JsonlTraceWriter, JsonSessionStore, ScriptedModel
from minidsh.adapters.conversations import JsonConversationRepository
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import (
    AgentBudget,
    Event,
    Message,
    Role,
    RunResult,
    ToolCall,
    redact_text,
    redact_value,
)
from minidsh.domain.harness import ApprovalChoice, ApprovalRequest
from minidsh.runtime.approvals import CallbackApproval
from minidsh.terminal import TerminalInput

_HELP = """/help             Show commands
/status           Show model, workspace, permissions and last run
/tools            Show registered tools and command permissions
/diff             Show the workspace Git diff
/test             Run the configured tests and keep their result for follow-ups
/key              Enter or update the provider API Key privately
/budget [KEY VALUE] Show or change run budgets (steps, tokens, tools, seconds)
/clear            Start a fresh conversation (files stay as they are)
/resume RUN_ID    Continue a saved run from this workspace
/sessions         List saved conversations and runs
/session ID       Continue a stable conversation
/fork RUN_ID      Fork history through an ended turn; files are not rolled back
/exit             Exit (also Ctrl+D)

Ctrl+C cancels the current task or clears the input.
Enter sends; Alt+Enter inserts a newline (or Escape, then Enter).
Type / to list commands; keep typing to filter; Tab completes.
Up/Down recalls input history.
Multiline paste stays in one draft until Enter sends it.
Ending a line with a backslash also inserts a newline.
"""


def _terminal_text(value: str) -> str:
    """Keep untrusted model/tool output from issuing terminal control commands."""
    return "".join(c for c in redact_text(value) if c in "\n\t" or c.isprintable())


def _write(value: str) -> None:
    sys.stdout.write(_terminal_text(value) + "\n")
    sys.stdout.flush()


_MILLISECONDS_PER_SECOND = 1000.0


def _format_duration(milliseconds: float) -> str:
    if milliseconds < _MILLISECONDS_PER_SECOND:
        return f"{milliseconds:.0f}ms"
    return f"{milliseconds / _MILLISECONDS_PER_SECOND:.1f}s"


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
        budget: AgentBudget | None = None,
        budget_applier: Callable[[AgentBudget], None] | None = None,
        credential_factory: Callable[[str], LocalRuntime] | None = None,
        credential_saver: Callable[[str], Awaitable[Path]] | None = None,
        credential_source: str | None = None,
        credential_required: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._factory = runtime_factory
        self._model_label = model_label
        self._options = options
        self._cost_reportable = cost_reportable
        self._resume = resume
        self._budget = budget or AgentBudget()
        self._budget_applier = budget_applier
        self._credential_factory = credential_factory
        self._credential_saver = credential_saver
        self._credential_source = credential_source
        self._credential_required = credential_required
        self._runtime: LocalRuntime | None = None
        self._history: tuple[Message, ...] = ()
        self._last_run: str | None = None
        self._changed: set[str] = set()
        self._verification = "not run this turn"
        self._last_summary = "no turns yet"
        self._sessions = JsonSessionStore(workspace)
        self._runner = asyncio.Runner()
        self._terminal = TerminalInput(status_provider=self._status_line)
        self._approvals = CallbackApproval(self._approve)
        self._streamed = False
        self._session_id: str | None = None
        self._conversation_repository = JsonConversationRepository(workspace)

    async def _approve(self, request: ApprovalRequest) -> ApprovalChoice:
        _write(request.preview)
        return await self._terminal.approve(request)

    def _bind(self, runtime: LocalRuntime) -> None:
        if self._options.approval_mode == "ask":
            binder = getattr(runtime.tools, "bind_approvals", None)
            if callable(binder):
                binder(self._approvals)

    def run(self) -> int:
        """Read tasks until EOF or /exit; failed turns leave the prompt available."""
        with self._runner:
            try:
                return self._prompt()
            finally:
                if self._runtime is not None and self._runtime.close is not None:
                    self._runner.run(self._await_callback(self._runtime.close()))

    @staticmethod
    async def _await_callback(operation: Awaitable[None]) -> None:
        await operation

    def select_session(self, session_id: str) -> None:
        """Validate a CLI-selected stable session before entering the prompt."""
        self._history = self._runner.run(self._conversation_repository.history(session_id))
        self._session_id = session_id

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

    def _read_task(self) -> str:
        return self._terminal.read_task()

    def _status(self) -> None:
        permissions = "write" if self._options.allow_write else "read-only"
        if self._options.allow_shell:
            if self._options.enabled_tools is None or "shell" in self._options.enabled_tools:
                permissions += f", shell ({', '.join(sorted(self._options.allowed_commands))})"
            else:
                permissions += ", configured commands only"
        _write(f"  Model: {self._model_label}\n  Workspace: {self._workspace}")
        if self._credential_source is not None:
            source = self._credential_source
            if self._credential_required and source == "not configured":
                source += f" (first task asks for {self._credential_required})"
            _write(f"  API Key: {source}")
        _write(f"  Permissions: {permissions}\n  Context: {self._options.context_strategy}")
        _write(f"  Last run: {self._last_run or '(none)'}")
        command = self._options.test_command
        _write(f"  Tests: {shlex.join(command) if command else '(not configured)'}")

    def _status_line(self) -> str:
        """One-line prompt toolbar: model, workspace, permissions and last turn."""
        permissions = "write" if self._options.allow_write else "read-only"
        if self._options.allow_shell:
            permissions += "+shell"
        workspace = self._workspace.name or str(self._workspace)
        return f" {self._model_label} · {workspace} · {permissions} · {self._last_summary}"

    def _budget_command(self, argument: str) -> None:
        if not argument:
            budget = self._budget
            _write(
                f"  Budget: {budget.max_steps} steps · {budget.max_tokens} tokens · "
                f"{budget.max_tool_calls} tool calls · {budget.max_duration_seconds:g}s"
            )
            _write("  Change: /budget KEY VALUE, keys: steps, tokens, tools, seconds")
            return
        try:
            key, raw = argument.split()
        except ValueError:
            _write("Usage: /budget KEY VALUE")
            return
        updated = self._parse_budget(key, raw)
        if updated is None:
            return
        if self._budget_applier is None:
            self._budget = updated
            _write("Budget updated for display only; this runtime cannot apply it.")
            return
        try:
            self._budget_applier(updated)
            self._reset_runtime()
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Cannot apply budget: {exc}")
            return
        self._budget = updated
        _write(
            f"Budget updated: {updated.max_steps} steps · {updated.max_tokens} tokens · "
            f"{updated.max_tool_calls} tool calls · {updated.max_duration_seconds:g}s "
            "(from the next task)"
        )

    def _parse_budget(self, key: str, raw: str) -> AgentBudget | None:  # noqa: PLR0911
        if key == "seconds":
            try:
                seconds = float(raw)
            except ValueError:
                _write(f"Invalid value for seconds: {raw}")
                return None
            if seconds <= 0:
                _write("Budget values must be positive.")
                return None
            return replace(self._budget, max_duration_seconds=seconds)
        if key not in {"steps", "tokens", "tools", "tool-calls"}:
            _write("Unknown budget key. Use steps, tokens, tools or seconds.")
            return None
        try:
            value = int(raw)
        except ValueError:
            _write(f"Invalid value for {key}: {raw}")
            return None
        if value <= 0:
            _write("Budget values must be positive.")
            return None
        if key == "steps":
            return replace(self._budget, max_steps=value)
        if key == "tokens":
            return replace(self._budget, max_tokens=value)
        return replace(self._budget, max_tool_calls=value)

    def _reset_runtime(self) -> None:
        runtime = self._runtime
        self._runtime = None
        if runtime is not None and runtime.close is not None:
            self._runner.run(self._await_callback(runtime.close()))

    def _command(self, task: str) -> bool:  # noqa: PLR0912 - explicit user command dispatch
        command, _, argument = task.partition(" ")
        argument = argument.strip()
        if command in {"/exit", "/quit"} and not argument:
            _write("Goodbye.")
            return False
        if command == "/resume" and argument:
            self._restore(argument)
        elif command == "/session" and argument:
            try:
                self.select_session(argument)
                _write(f"Resumed session {argument}.")
            except (OSError, ValueError, RuntimeError) as exc:
                _write(f"Cannot resume session: {exc}")
        elif command == "/fork" and argument:
            self._fork(argument)
        elif command == "/budget":
            self._budget_command(argument)
        elif argument:
            _write("Unknown command or arguments. Type /help.")
        elif command == "/help":
            _write(_HELP)
        elif command == "/status":
            self._status()
        elif command == "/tools":
            self._tools()
        elif command == "/clear":
            self._history = ()
            self._last_run = None
            self._session_id = None
            _write("Conversation cleared. Workspace files are unchanged.")
        elif command == "/diff":
            self._diff()
        elif command == "/test":
            self._test()
        elif command == "/key":
            self._key()
        elif command == "/sessions":
            self._list_sessions()
        else:
            _write("Unknown command or missing argument. Type /help.")
        return True

    def _fork(self, run_id: str) -> None:
        if self._ensure_runtime() and self._runtime and self._runtime.conversations:
            try:
                self._session_id = self._runner.run(self._runtime.conversations.fork(run_id))
                self._history = self._runner.run(
                    self._runtime.conversations.history(self._session_id)
                )
                _write(f"Forked into {self._session_id}; workspace files were not rolled back.")
            except (OSError, ValueError, RuntimeError) as exc:
                _write(f"Cannot fork: {exc}")

    def _list_sessions(self) -> None:
        try:
            summaries = self._runner.run(self._conversation_repository.list())
            for summary in summaries:
                _write(
                    f"{summary.session_id} · {summary.turn_count} turns · "
                    f"{summary.status} · {summary.title}"
                )
            for result in self._runner.run(self._sessions.list()):
                if not result.session_id:
                    _write(f"Legacy run {result.run_id} · {result.status.value}")
            if not summaries:
                _write("No stable sessions yet.")
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Cannot list sessions: {exc}")

    def _tools(self) -> None:
        # Local capability discovery needs no model credential or model call.
        options = replace(
            self._options,
            multi_agent_enabled=False,
            enabled_tools=(
                self._options.enabled_tools - {"delegate_sub_agents"}
                if self._options.enabled_tools is not None
                else None
            ),
        )
        try:
            runtime = self._runtime or build_local_runtime(
                workspace=self._workspace, model=ScriptedModel([]), options=options
            )
            names = sorted(definition.name for definition in runtime.tools.definitions())
            _write(f"Registered tools: {', '.join(names) or '(none)'}")
            _write(f"Execution backend: {self._options.sandbox_backend}")
            if self._runtime is None:
                _write("Local tool preview; provider extensions are listed after startup.")
            if self._options.allow_shell:
                _write(f"Allowed commands: {', '.join(sorted(self._options.allowed_commands))}")
            if "shell" not in names:
                _write("General commands need --allow-shell --allow-command EXECUTABLE at startup.")
            if self._options.denied_tools:
                _write(f"Denied tools: {', '.join(sorted(self._options.denied_tools))}")
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Tools unavailable: {exc}")

    def _restore(self, run_id: str) -> None:
        try:
            result = self._runner.run(self._sessions.load(run_id))
            if result is None:
                _write(f"No saved run: {run_id}")
                return
            self._history = result.history
            self._last_run = result.run_id
            self._session_id = result.session_id or None
            if result.session_id:
                summaries = self._runner.run(self._conversation_repository.list())
                latest = next(
                    (
                        item.latest_run_id
                        for item in summaries
                        if item.session_id == result.session_id
                    ),
                    None,
                )
                if latest != run_id:
                    self._session_id = self._runner.run(self._conversation_repository.fork(run_id))
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

    def _ensure_runtime(self) -> bool:
        if self._runtime is None and self._credential_required is not None:
            _write(f"API Key missing ({self._credential_required}). Configure it to run this task.")
            if not self._key():
                return False
        try:
            if self._runtime is None:
                self._runtime = self._factory()
                self._bind(self._runtime)
                self._runtime.events.subscribe("*", self._progress)
                if self._options.streaming_enabled and not self._runtime.streaming_capable:
                    _write("Model uses complete-response mode; incremental output is unavailable.")
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Cannot start: {exc}")
            if self._credential_factory is not None:
                _write("Use /key to enter a provider key privately.")
            return False
        return True

    def _key(self) -> bool:
        if self._credential_factory is None:
            _write("Key entry is unavailable for this model. Configure its provider environment.")
            return False
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                prompt = (
                    "API Key (hidden; saved to workspace credentials.toml): "
                    if self._credential_saver is not None
                    else "API Key (hidden, memory only): "
                )
                key = getpass.getpass(prompt).strip()
            if not key:
                _write("Key unchanged.")
                return False
            runtime = self._credential_factory(key)
            if runtime.initialize is not None:
                self._runner.run(self._await_callback(runtime.initialize()))
            if self._runtime is not None and self._runtime.close is not None:
                self._runner.run(self._await_callback(self._runtime.close()))
            self._bind(runtime)
            runtime.events.subscribe("*", self._progress)
            self._runtime = runtime
            self._credential_required = None
            self._credential_source = "session"
            if self._credential_saver is not None:
                try:
                    path = self._runner.run(self._save_key(key))
                    self._credential_source = "workspace config"
                    _write(f"Key saved to {path}. No model request has been sent.")
                except (OSError, ValueError, RuntimeError):
                    _write(
                        "Key configured for this session, but could not be saved to the workspace."
                    )
            else:
                _write("Key configured for this session. No model request has been sent.")
            return True
        except (EOFError, KeyboardInterrupt):
            _write("Key entry cancelled.")
        except getpass.GetPassWarning:
            _write("This terminal cannot hide input. Configure the provider environment instead.")
        except (OSError, ValueError, RuntimeError):
            _write("Cannot configure the provider. Check its profile and runtime settings.")
        return False

    async def _save_key(self, key: str) -> Path:
        assert self._credential_saver is not None
        return await self._credential_saver(key)

    def _test(self) -> None:
        if not self._options.allow_shell or self._options.test_command is None:
            _write(
                "Tests are unavailable. Start with --test-command and command execution enabled."
            )
            return
        # Local tests need no model request or provider credential.
        runtime = build_local_runtime(
            workspace=self._workspace,
            model=ScriptedModel([]),
            options=replace(self._options, approval_mode="configured"),
        )
        runtime.events.subscribe("*", self._progress)
        run_id = uuid.uuid4().hex
        trace = self._workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
        unsubscribe = runtime.events.subscribe("*", JsonlTraceWriter(trace))
        try:
            observation = self._runner.run(
                runtime.tools.execute(ToolCall(uuid.uuid4().hex, "test", {}), run_id, 0)
            )
            feedback = json.dumps(
                {
                    "status": observation.status.value,
                    "result": observation.result,
                    "error": observation.error.message if observation.error else None,
                },
                ensure_ascii=False,
            )
            if not self._history:
                self._history = (Message(Role.SYSTEM, self._options.system_prompt),)
            self._history = (
                *self._history,
                Message(
                    Role.SYSTEM,
                    "User-invoked test result (observed, not self-reported):\n" + feedback,
                    name="verification_status",
                ),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            _write("Test cancelled; command cleanup completed.")
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Test unavailable: {exc}")
        finally:
            unsubscribe()
            _write(f"  Test trace: {trace}")

    def _turn(self, task: str) -> None:
        if not self._terminal.enhanced:
            _write(f"  Request: {task}")
        if not self._ensure_runtime():
            return
        runtime = self._runtime
        assert runtime is not None
        self._changed = set()
        self._verification = "not run this turn"
        self._streamed = False
        self._last_summary = "running turn"
        run_id = uuid.uuid4().hex
        trace = self._workspace / ".minidsh" / "traces" / f"{run_id}.jsonl"
        unsubscribe = runtime.events.subscribe("*", JsonlTraceWriter(trace))
        try:
            if runtime.conversations is not None:
                if self._session_id is None and self._last_run is not None:
                    self._session_id = self._runner.run(
                        runtime.conversations.resume_run(self._last_run)
                    )
                operation = runtime.conversations.run_turn(
                    task, session_id=self._session_id, run_id=run_id, seed=self._history
                )
            else:
                operation = runtime.agent.run(task, run_id=run_id, prior_history=self._history)
            result = self._runner.run(operation)
            self._history = result.history
            self._last_run = result.run_id
            self._session_id = result.session_id or None
            self._show_result(result)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _write("\nTask cancelled. Returning to the prompt.")
            self._last_summary = "cancelled"
            # AgentLoop persists its cancelled state before propagating cancellation.
            self._restore(run_id)
        except (OSError, ValueError, RuntimeError) as exc:
            _write(f"Task failed: {exc}")
            self._last_summary = "failed"
        finally:
            self._approvals.release(run_id)
            unsubscribe()
            _write(f"  Run: {run_id}\n  Trace: {trace}\n")

    def _show_result(self, result: RunResult) -> None:
        if self._streamed:
            sys.stdout.write("\n")
        if result.answer and not self._streamed:
            _write(f"\nassistant > {result.answer}\n")
        if result.failure_reason:
            _write(f"  {result.status.value}: {result.failure_reason}")
        if self._changed:
            _write(f"  Changed: {', '.join(sorted(self._changed))}")
        if self._options.test_command is not None or self._changed:
            _write(f"  Verification: {self._verification}")
        cost = f"${result.cost_usd:.6f}" if self._cost_reportable else "unavailable (no rate card)"
        self._last_summary = (
            f"{result.status.value} · {result.steps} steps · "
            f"{result.token_usage.total_tokens} tokens"
        )
        _write(
            f"  {result.status.value} · {result.steps} steps · {result.tool_calls} tools · "
            f"{result.token_usage.total_tokens} tokens · {result.duration_ms / 1000:.1f}s · "
            f"cost {cost}"
        )

    async def _progress(self, event: Event) -> None:
        if event.name == "model_text_received":
            text = event.data.get("text")
            if isinstance(text, str):
                if not self._streamed:
                    sys.stdout.write("\nassistant > ")
                self._streamed = True
                sys.stdout.write(_terminal_text(text))
                sys.stdout.flush()
        elif event.name == "sub_agent_started":
            _write(f"  Sub Agent {event.data.get('name')} ({event.data.get('role')}): started")
        elif event.name == "sub_agent_finished":
            _write(f"  Sub Agent {event.data.get('name')}: {event.data.get('status')}")
        elif (
            event.name == "model_call_completed"
            and event.data.get("tool_call_count")
            and not self._streamed
        ):
            content = event.data.get("content")
            if isinstance(content, str) and content:
                _write(content[:2000])
        elif event.name == "tool_call_started":
            arguments = json.dumps(redact_value(event.data.get("arguments")), ensure_ascii=False)
            _write(f"  → {event.data.get('tool')} {arguments[:240]}")
        elif event.name == "tool_call_completed":
            self._tool_completed(event)
        elif event.name == "session_save_failed":
            _write("  Session could not be saved; this turn cannot be resumed from disk.")

    def _tool_completed(self, event: Event) -> None:
        tool = event.data.get("tool")
        result = event.data.get("result")
        status = event.data.get("status")
        error = event.data.get("error")
        duration = event.data.get("duration_ms")
        timing = (
            f" · {_format_duration(float(duration))}"
            if isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and duration >= 0
            else ""
        )
        error_details = error.get("details") if isinstance(error, dict) else None
        executed = status == "succeeded" or (
            isinstance(error_details, dict) and error_details.get("execution_succeeded") is True
        )
        if (
            event.data.get("call_id") == "workspace-instructions"
            and tool == "read_file"
            and isinstance(error, dict)
            and error.get("code") == "file_not_found"
        ):
            _write("  Project instructions: optional AGENTS.md not found.")
            return
        if tool == "test" and isinstance(result, dict) and type(result.get("exit_code")) is int:
            code = result["exit_code"]
            if result.get("timed_out") is True:
                self._verification = "timed out"
            else:
                passed = status == "succeeded" and code == 0
                self._verification = "passed" if passed else f"failed (exit {code})"
            _write(f"  ← test: {self._verification}{timing}")
            for stream in ("stdout", "stderr"):
                value = result.get(stream)
                if isinstance(value, str) and value:
                    _write(value[-4000:])
        else:
            _write(f"  ← {tool}: {status}{timing}")
            if tool == "test":
                self._verification = f"not verified ({status})"
        if executed and tool in {"patch_file", "write_file"}:
            if isinstance(result, dict) and isinstance(result.get("path"), str):
                self._changed.add(str(result["path"]))
            self._verification = "not run after the latest edit"
        if executed and tool in {"shell", "build"}:
            self._verification = "not run after the latest command"
        if isinstance(error, dict):
            _write(f"    {error.get('message', '')}")

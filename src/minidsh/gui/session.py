"""Bounded browser views over the existing single-agent runtime."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from minidsh.adapters import JsonlTraceWriter, JsonSessionStore, ScriptedModel
from minidsh.composition import (
    MAX_SUBAGENT_PARALLEL,
    MAX_SUBAGENTS,
    LocalRuntime,
    RuntimeOptions,
    build_local_runtime,
)
from minidsh.domain import Event, Message, Role, RunResult, ToolCall, redact_text, redact_value

_VIEW_LIMIT = 20_000


def _preview(text: str) -> str:
    value = redact_text(text)
    return value if len(value) <= _VIEW_LIMIT else value[:_VIEW_LIMIT] + "\n[展示已截断]"


class _EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TaskInput(_EmptyInput):
    task: str = Field(min_length=1, max_length=32_000)


class _ResumeInput(_EmptyInput):
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class _SettingsInput(_EmptyInput):
    multi_agent_enabled: bool
    subagent_max_agents: int = Field(ge=1, le=MAX_SUBAGENTS)
    subagent_max_parallel: int = Field(ge=1, le=MAX_SUBAGENT_PARALLEL)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class GuiSession:
    """A single conversation, accessed only from its owning asyncio loop."""

    def __init__(  # noqa: PLR0913 - explicit UI adapters and credential capabilities
        self,
        *,
        workspace: Path,
        runtime_factory: Callable[[], LocalRuntime],
        model_label: str,
        options: RuntimeOptions,
        cost_reportable: bool,
        settings_factory: Callable[[RuntimeOptions, str | None], LocalRuntime] | None = None,
        credentials_editable: bool = False,
        api_key_available: bool = False,
    ) -> None:
        self.workspace = workspace
        self._factory = runtime_factory
        self._model_label = model_label
        self._options = options
        self._priced = cost_reportable
        self._settings_factory = settings_factory
        self._credentials_editable = credentials_editable
        self._api_key_available = api_key_available
        self._api_key: SecretStr | None = None
        self._sub_agents: dict[str, dict[str, Any]] = {}
        self._action_lock = asyncio.Lock()
        self._runtime: LocalRuntime | None = None
        self._sessions = JsonSessionStore(workspace)
        self._history: tuple[Message, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._messages: deque[dict[str, str]] = deque(maxlen=100)
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._event_count = 0
        self._message_count = 0
        self._status = "idle"
        self._run_id: str | None = None
        self._error: str | None = None
        self._metrics: dict[str, Any] = {}
        self._diff = ""

    @property
    def busy(self) -> bool:
        """Whether an operation owns the runtime."""
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        """Return a redacted, bounded derived view; traces remain the source of truth."""
        return {
            "workspace": str(self.workspace),
            "model": self._model_label,
            "context": self._options.context_strategy,
            "multi_agent_enabled": self._options.multi_agent_enabled,
            "collaboration": {
                "editable": self._settings_factory is not None,
                "max_agents": self._options.subagent_max_agents,
                "max_parallel": self._options.subagent_max_parallel,
            },
            "credentials": {
                "editable": self._credentials_editable,
                "configured": self._api_key is not None or self._api_key_available,
            },
            "sub_agents": list(self._sub_agents.values()),
            "permissions": {
                "write": self._options.allow_write,
                "shell": self._options.allow_shell,
                "commands": sorted(self._options.allowed_commands),
            },
            "busy": self.busy,
            "status": self._status,
            "run_id": self._run_id,
            "messages": list(self._messages),
            "events": list(self._events),
            "events_truncated": self._event_count > len(self._events),
            "messages_truncated": self._message_count > len(self._messages),
            "error": self._error,
            "metrics": self._metrics,
            "diff": self._diff,
            "trace": f".minidsh/traces/{self._run_id}.jsonl" if self._run_id else None,
        }

    async def action(self, action: str, data: dict[str, Any]) -> None:
        """Validate browser input before scheduling any runtime operation."""
        async with self._action_lock:
            await self._action(action, data)

    async def _action(self, action: str, data: dict[str, Any]) -> None:
        if action == "cancel":
            _EmptyInput.model_validate(data)
            if self.busy and self._task is not None:
                self._task.cancel()
            return
        if self.busy:
            raise ValueError("A task is running; stop it or wait before continuing.")
        if action == "run":
            task = _TaskInput.model_validate(data).task.strip()
            if not task:
                raise ValueError("task must not be blank")
            self._error = None
            self._metrics = {}
            self._events.clear()
            self._sub_agents.clear()
            self._event_count = 0
            self._run_id = uuid.uuid4().hex
            self._status = "running"
            self._message("user", task)
            self._task = asyncio.create_task(self._execute(task))
            self._task.add_done_callback(self._operation_done)
        elif action == "resume":
            run_id = _ResumeInput.model_validate(data).run_id
            result = await self._sessions.load(run_id)
            if result is None:
                raise ValueError("No saved session for this run_id")
            self._restore(result)
        elif action == "settings":
            self._update_settings(data)
        elif action == "clear":
            _EmptyInput.model_validate(data)
            self._history = ()
            self._messages.clear()
            self._events.clear()
            self._sub_agents.clear()
            self._message_count = self._event_count = 0
            self._run_id = None
            self._status = "idle"
            self._error = None
            self._metrics = {}
            self._diff = ""
        elif action == "diff":
            _EmptyInput.model_validate(data)
            self._error = None
            self._diff = ""
            self._task = asyncio.create_task(self._show_diff())
            self._task.add_done_callback(self._operation_done)
        else:
            raise ValueError("Unknown action")

    def _update_settings(self, data: dict[str, Any]) -> None:
        selected = _SettingsInput.model_validate(data)
        if self._settings_factory is None:
            raise ValueError("Runtime settings are fixed by the embedding application")
        if selected.api_key is not None and not self._credentials_editable:
            raise ValueError("API Key input is unavailable for this model")
        secret = selected.api_key if selected.api_key is not None else self._api_key
        if secret is not None and not secret.get_secret_value().strip():
            raise ValueError("API Key must not be blank")
        enabled = self._options.enabled_tools
        if enabled is not None:
            enabled = (
                enabled | {"delegate_sub_agents"}
                if selected.multi_agent_enabled
                else enabled - {"delegate_sub_agents"}
            )
        options = replace(
            self._options,
            multi_agent_enabled=selected.multi_agent_enabled,
            subagent_max_agents=selected.subagent_max_agents,
            subagent_max_parallel=selected.subagent_max_parallel,
            enabled_tools=enabled,
        )
        # Construct successfully before committing changes; credentials never enter options.
        runtime = self._settings_factory(options, secret.get_secret_value() if secret else None)
        self._options, self._runtime, self._api_key = options, runtime, secret
        self._error = None

    def _operation_done(self, task: asyncio.Task[None]) -> None:
        if task is not self._task:
            return
        if task.cancelled():
            self._status = "cancelled"
        elif (error := task.exception()) is not None:
            self._status = "failed"
            self._error = _preview(str(error))

    async def wait(self) -> None:
        """Wait for the current operation to release the runtime."""
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                # A task cancelled before its coroutine starts has no side effects.
                self._status = "cancelled"

    async def close(self) -> None:
        """Cancel and drain the current operation during server shutdown."""
        if self.busy and self._task is not None:
            self._task.cancel()
        await self.wait()
        self._api_key = None
        self._runtime = None

    def _message(self, role: str, content: str) -> None:
        self._message_count += 1
        self._messages.append({"role": role, "content": _preview(content)})

    def _restore(self, result: RunResult) -> None:
        self._history = result.history
        self._run_id = result.run_id
        self._messages.clear()
        self._message_count = 0
        for message in result.history:
            if message.role in {Role.USER, Role.ASSISTANT} and message.content:
                self._message(message.role.value, message.content)
        self._events.clear()
        self._sub_agents.clear()
        self._event_count = 0
        self._diff = ""
        self._result(result)

    def _result(self, result: RunResult) -> None:
        self._status = result.status.value
        self._error = _preview(result.failure_reason) if result.failure_reason else None
        self._metrics = {
            "steps": result.steps,
            "tools": result.tool_calls,
            "tokens": result.token_usage.total_tokens,
            "seconds": round(result.duration_ms / 1000, 2),
            "cost_usd": result.cost_usd if self._priced else None,
        }

    async def _progress(self, event: Event) -> None:
        self._agent_progress(event)
        self._event_count += 1
        self._events.append(
            {
                "name": event.name,
                "run_id": event.run_id,
                "step_id": event.step_id,
                "data": _preview(json.dumps(redact_value(event.data), ensure_ascii=False)),
            }
        )

    def _agent_progress(self, event: Event) -> None:
        child_id = (
            event.data.get("child_run_id") if event.name.startswith("sub_agent_") else event.run_id
        )
        if not isinstance(child_id, str):
            return
        if event.name == "sub_agent_started" and len(self._sub_agents) < MAX_SUBAGENTS:
            self._sub_agents[child_id] = {
                "run_id": child_id,
                "name": _preview(str(event.data.get("name", ""))),
                "role": _preview(str(event.data.get("role", ""))),
                "status": "running",
                "answer": "",
                "tokens": 0,
            }
        agent = self._sub_agents.get(child_id)
        if agent is None:
            return
        if event.name == "model_call_completed" and not event.data.get("tool_call_count"):
            agent["answer"] = _preview(str(event.data.get("content", "")))[:4000]
        if event.name in {"sub_agent_finished", "run_finished"}:
            agent["status"] = str(event.data.get("status", "failed"))
            if event.data.get("failure_reason"):
                agent["error"] = _preview(str(event.data["failure_reason"]))
        if event.name == "run_finished":
            agent["tokens"] = event.data.get("tokens", 0)

    async def _execute(self, task: str) -> None:
        def noop() -> None:
            pass

        unsubscribe: Callable[[], None] = noop
        progress: Callable[[], None] = noop
        run_id = self._run_id
        assert run_id is not None
        try:
            if self._runtime is None:
                self._runtime = self._factory()
            runtime = self._runtime
            unsubscribe = runtime.events.subscribe(
                "*", JsonlTraceWriter(self.workspace / ".minidsh" / "traces" / f"{run_id}.jsonl")
            )
            progress = runtime.events.subscribe("*", self._progress)
            result = await runtime.agent.run(task, run_id=run_id, prior_history=self._history)
            self._history = result.history
            self._result(result)
            if result.answer:
                self._message("assistant", result.answer)
        except asyncio.CancelledError:
            self._status = "cancelled"
            try:
                saved = await self._sessions.load(run_id)
                if saved is not None:
                    self._history = saved.history
                    self._result(saved)
            except (OSError, ValueError, RuntimeError) as exc:
                self._error = _preview(str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            self._status = "failed"
            self._error = _preview(str(exc))
        finally:
            unsubscribe()
            progress()

    async def _show_diff(self) -> None:
        options = replace(
            self._options,
            multi_agent_enabled=False,
            enabled_tools=(
                self._options.enabled_tools - {"delegate_sub_agents"}
                if self._options.enabled_tools is not None
                else None
            ),
        )
        runtime = self._runtime or build_local_runtime(
            workspace=self.workspace, model=ScriptedModel([]), options=options
        )
        run_id = uuid.uuid4().hex
        unsubscribe = runtime.events.subscribe(
            "*", JsonlTraceWriter(self.workspace / ".minidsh" / "traces" / f"{run_id}.jsonl")
        )
        try:
            observation = await runtime.tools.execute(
                ToolCall(uuid.uuid4().hex, "git", {"action": "diff", "paths": []}), run_id, 0
            )
            if observation.error:
                self._error = _preview(observation.error.message)
            elif isinstance(observation.result, dict):
                result = observation.result
                if observation.truncated:
                    self._diff = _preview(str(result.get("preview", "")))
                    self._diff += "\n[工具输出已截断]"
                    if observation.artifacts:
                        self._diff += "\n完整输出: " + ", ".join(observation.artifacts)
                else:
                    output = (
                        result.get("stdout")
                        if result.get("exit_code") == 0
                        else result.get("stderr")
                    )
                    self._diff = _preview(str(output or "没有已跟踪文件的改动。"))
        except asyncio.CancelledError:
            self._error = "Diff 已取消"
        except (OSError, ValueError, RuntimeError) as exc:
            self._error = _preview(str(exc))
        finally:
            unsubscribe()

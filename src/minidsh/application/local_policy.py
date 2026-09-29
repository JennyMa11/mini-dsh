"""Small-model plan state and deterministic repeated-action detection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from minidsh.domain import ToolCall, ToolObservation, ToolStatus

_RECOVERY_THRESHOLD = 3
_STOP_THRESHOLD = 6
_RECOVERY_HINTS = {
    "patch_mismatch": "Read the current file range before making a new patch.",
    "patch_ambiguous": "Inspect occurrences and specify the intended one.",
    "command_failed": "Check the executable and the configured command allowlist.",
    "tool_timeout": "Narrow the operation before retrying.",
    "invalid_arguments": "Use the tool's declared input schema.",
}
_PYTHON_ERROR = re.compile(r"\b([A-Za-z]+Error):")


@dataclass(slots=True)
class LocalPlanState:
    goal: str
    recovery_enabled: bool = True
    current_step: str = "inspect"
    completed_steps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    recovery_hint: str = ""
    unresolved_edit: bool = False

    def observe(self, call: ToolCall, observation: ToolObservation) -> None:
        """Advance a compact factual plan without rewriting the full task."""
        if observation.status is not ToolStatus.SUCCEEDED:
            if call.name in {"patch_file", "write_file"}:
                self.unresolved_edit = True
            code = (
                observation.error.code
                if observation.error is not None
                else observation.status.value
            )
            self.blockers = [f"{call.name}: {code}"]
            self.current_step = "recover"
            self.recovery_hint = (
                _RECOVERY_HINTS.get(code, "Inspect the error before changing strategy.")
                if self.recovery_enabled
                else ""
            )
            return
        if isinstance(observation.result, dict):
            exit_code = observation.result.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                stderr = observation.result.get("stderr")
                error = _PYTHON_ERROR.search(stderr) if isinstance(stderr, str) else None
                code = error.group(1) if error is not None else f"exit_code={exit_code}"
                self.blockers = [f"{call.name}: {code}"]
                self.current_step = "recover"
                self.recovery_hint = (
                    (
                        "Read the exact failing lines and fix Python syntax before testing again."
                        if code in {"IndentationError", "SyntaxError"}
                        else "Inspect the failing test diagnostics and relevant code."
                    )
                    if self.recovery_enabled
                    else ""
                )
                return
        if not self.unresolved_edit or call.name in {"patch_file", "write_file"}:
            self.blockers.clear()
            self.recovery_hint = ""
        if call.name in {"patch_file", "write_file"}:
            self.unresolved_edit = False
            path = call.arguments.get("path")
            if isinstance(path, str) and path not in self.modified_files:
                self.modified_files.append(path)
            self._advance("edit", "test")
        elif call.name in {"test", "build", "shell"}:
            self._advance("test", "finish")
        else:
            self._advance("inspect", "edit")

    def _advance(self, completed: str, next_step: str) -> None:
        if completed not in self.completed_steps:
            self.completed_steps.append(completed)
        self.current_step = next_step

    def render(self) -> str:
        return json.dumps(
            {
                "goal": self.goal,
                "current_step": self.current_step,
                "completed_steps": self.completed_steps,
                "blockers": self.blockers,
                "modified_files": self.modified_files,
                "recovery_hint": self.recovery_hint,
                "unresolved_edit": self.unresolved_edit,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(slots=True)
class RepetitionGuard:
    """Recover at three equivalent observations; stop at six."""

    include_errors: bool = True
    signature: str = ""
    count: int = 0
    error_signature: str = ""
    error_count: int = 0

    def observe(
        self, call: ToolCall, observation: ToolObservation
    ) -> Literal["continue", "recover", "stop"]:
        signature = json.dumps(
            {
                "tool": call.name,
                "args": call.arguments,
                "status": observation.status.value,
                "result": observation.result,
                "error": observation.error.code if observation.error is not None else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.count = self.count + 1 if signature == self.signature else 1
        self.signature = signature
        error = self._error_signature(call, observation) if self.include_errors else ""
        if error:
            self.error_count = self.error_count + 1 if error == self.error_signature else 1
            self.error_signature = error
        elif (
            call.name in {"test", "build", "shell"}
            and isinstance(observation.result, dict)
            and observation.result.get("exit_code") == 0
        ):
            self.error_count = 0
            self.error_signature = ""
        if self.count >= _STOP_THRESHOLD or self.error_count >= _STOP_THRESHOLD:
            return "stop"
        if self.count == _RECOVERY_THRESHOLD or (error and self.error_count == _RECOVERY_THRESHOLD):
            return "recover"
        return "continue"

    @staticmethod
    def _error_signature(call: ToolCall, observation: ToolObservation) -> str:
        if observation.error is not None:
            return f"{call.name}:{observation.error.code}"
        if isinstance(observation.result, dict):
            exit_code = observation.result.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                stderr = observation.result.get("stderr")
                match = _PYTHON_ERROR.search(stderr) if isinstance(stderr, str) else None
                return f"{call.name}:{match.group(1) if match else exit_code}"
            if exit_code == 0:
                return ""
        return ""

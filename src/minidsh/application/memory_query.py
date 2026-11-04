"""Bounded Memory matching hints from causal tool history, without task-text guessing."""

import json
import re
from pathlib import PurePosixPath

from minidsh.domain import JsonValue, Message, Role
from minidsh.domain.memory import (
    MAX_MEMORY_ERROR_CHARS,
    MAX_MEMORY_PATH_CHARS,
    MAX_MEMORY_PATHS,
)

_FILES = frozenset(
    {"read_file", "write_file", "patch_file", "find_symbol", "search_code", "list_files"}
)
_ERROR = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*Error):")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


def _path(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_MEMORY_PATH_CHARS:
        return None
    path = PurePosixPath(value)
    if (
        value == "."
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
        or path.as_posix() != value
    ):
        return None
    return value


def _failure(payload: dict[str, JsonValue]) -> str | None:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and _CODE.fullmatch(code) is not None:
        return code
    result = payload.get("result")
    if (
        isinstance(result, dict)
        and type(result.get("exit_code")) is int
        and result["exit_code"] != 0
    ):
        for key in ("stderr", "diagnostics"):
            detail = result.get(key)
            found = _ERROR.search(detail) if isinstance(detail, str) else None
            if found is not None and len(found.group(1)) <= MAX_MEMORY_ERROR_CHARS:
                return found.group(1)
        return "command_failed"
    status = payload.get("status")
    if isinstance(status, str) and status in {"failed", "denied", "timed_out", "invalid"}:
        return status
    return None


def memory_query(history: tuple[Message, ...]) -> tuple[tuple[str, ...], str | None]:
    """Use the latest 32 canonical accessed paths and the last unresolved tool failure."""
    paths: list[str] = []
    calls: dict[str, str] = {}
    error_type: str | None = None
    error_tool: str | None = None
    for message in history:
        for call in message.tool_calls:
            calls[call.call_id] = call.name
            path = _path(call.arguments.get("path")) if call.name in _FILES else None
            if path is not None:
                if path in paths:
                    paths.remove(path)
                paths.append(path)
                paths = paths[-MAX_MEMORY_PATHS:]
        name = calls.get(message.tool_call_id or "")
        if message.role is not Role.TOOL or name is None:
            continue
        try:
            payload: JsonValue = json.loads(message.content)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        failed = _failure(payload)
        if failed is not None:
            error_type, error_tool = failed, name
        elif payload.get("status") == "succeeded" and name == error_tool:
            error_type, error_tool = None, None
    return tuple(sorted(paths)), error_type

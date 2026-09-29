"""Deterministic extraction of useful diagnostics from oversized tool output."""

from __future__ import annotations

import re

from minidsh.domain import JsonValue

_ERROR = re.compile(r"(error|failed|failure|traceback|exception|assertion|not found)", re.I)


def summarize(name: str, result: JsonValue) -> str:
    if isinstance(result, dict):
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int):
            return f"{name} exited {exit_code}"
        matches = result.get("matches") or result.get("candidates") or result.get("symbols")
        if isinstance(matches, list):
            return f"{name} returned {len(matches)} items"
        path = result.get("path")
        if isinstance(path, str):
            return f"{name}: {path}"
    return f"{name} completed"


def diagnostics(result: JsonValue, *, limit: int = 10) -> list[str]:
    """Keep distinct error lines in source order, without shell-specific parsing."""
    if not isinstance(result, dict):
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for key in ("stderr", "stdout"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            normalized = line.strip()
            if not normalized or not _ERROR.search(normalized) or normalized in seen:
                continue
            seen.add(normalized)
            lines.append(normalized[:300])
            if len(lines) >= limit:
                return lines
    return lines

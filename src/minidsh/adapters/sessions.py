"""Atomic JSON session persistence for resumable inspection."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.adapters.session_codec import StoredMessage, message_to_json
from minidsh.adapters.session_journal import read_session_log
from minidsh.domain import (
    LatencyBreakdown,
    Message,
    Role,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    redact_value,
)


class SessionStoreError(RuntimeError):
    """Invalid or unavailable session persistence."""


class JsonSessionStore:
    """Store each completed run as one atomic JSON document."""

    def __init__(self, workspace: Path) -> None:
        root = workspace.resolve(strict=True)
        self._directory = root / ".minidsh" / "sessions"

    async def save(self, result: RunResult) -> None:
        """Serialize and atomically replace one run record."""
        await asyncio.to_thread(self._save_sync, result)

    async def load(self, run_id: str) -> RunResult | None:
        """Validate and reconstruct one run record."""
        self._validate_run_id(run_id)
        return await asyncio.to_thread(self._load_sync, run_id)

    def _save_sync(self, result: RunResult) -> None:
        self._validate_run_id(result.run_id)
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._directory / f"{result.run_id}.json"
        payload = {
            "schema_version": "1.1",
            "run_id": result.run_id,
            "status": result.status.value,
            "answer": result.answer,
            "steps": result.steps,
            "input_tokens": result.token_usage.input_tokens,
            "output_tokens": result.token_usage.output_tokens,
            "cost_usd": result.token_usage.cost_usd,
            "cached_input_tokens": result.token_usage.cached_input_tokens,
            "latency": asdict(result.latency),
            "tool_calls": result.tool_calls,
            "duration_ms": result.duration_ms,
            "failure_reason": result.failure_reason,
            "history": [message_to_json(message) for message in result.history],
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=".session-", dir=self._directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(redact_value(payload), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _load_sync(self, run_id: str) -> RunResult | None:
        path = self._directory / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            parsed = _StoredRun.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            msg = f"invalid stored session: {run_id}"
            raise SessionStoreError(msg) from exc
        history = tuple(
            Message(
                Role(message.role),
                message.content,
                name=message.name,
                reasoning_content=message.reasoning_content,
                tool_call_id=message.tool_call_id,
                tool_calls=tuple(
                    ToolCall(call.call_id, call.name, call.arguments) for call in message.tool_calls
                ),
            )
            for message in parsed.history
        )
        # Old snapshots remain readable; new transcript views derive from facts.
        journal_path = self._directory.parent / "session_events" / f"{run_id}.jsonl"
        if not journal_path.resolve().is_relative_to(self._directory.parent.parent):
            raise SessionStoreError("session event log escapes workspace")
        if journal_path.exists():
            try:
                history = read_session_log(journal_path, run_id).messages()
            except (OSError, ValueError) as exc:
                raise SessionStoreError("invalid stored session event log") from exc
        return RunResult(
            parsed.run_id,
            RunStatus(parsed.status),
            parsed.answer,
            parsed.steps,
            TokenUsage(
                parsed.input_tokens,
                parsed.output_tokens,
                parsed.cost_usd,
                parsed.cached_input_tokens,
            ),
            parsed.tool_calls,
            parsed.duration_ms,
            parsed.failure_reason,
            history,
            LatencyBreakdown(**parsed.latency.model_dump()),
        )

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not run_id or not run_id.replace("-", "").replace("_", "").isalnum():
            msg = "run_id contains unsafe characters"
            raise SessionStoreError(msg)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StoredLatency(_StrictModel):
    model_ms: float = 0.0
    tool_ms: float = 0.0
    harness_ms: float = 0.0
    total_ms: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0


class _StoredRun(_StrictModel):
    schema_version: str
    run_id: str
    status: str
    answer: str
    steps: int
    input_tokens: int
    output_tokens: int
    cost_usd: float = 0.0
    cached_input_tokens: int = 0
    latency: _StoredLatency = Field(default_factory=_StoredLatency)
    tool_calls: int
    duration_ms: float
    failure_reason: str | None
    history: list[StoredMessage]

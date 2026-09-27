"""Typed, read-only view over a JSONL runtime trace.

The trace file is the fact source. Everything else in this package — latency,
failure diagnosis, replay, diff — is a *view* computed from these records and can be
regenerated at any time without re-running a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.domain import JsonValue


class TraceError(RuntimeError):
    """Unreadable or malformed trace file."""


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One durable runtime fact."""

    name: str
    run_id: str
    step_id: int
    data: dict[str, JsonValue] = field(default_factory=dict)
    occurred_at: str = ""
    schema_version: str = "1.0"


@dataclass(frozen=True, slots=True)
class TraceStep:
    """All facts belonging to one agent step, in execution order."""

    step_id: int
    context: TraceRecord | None = None
    model_call: TraceRecord | None = None
    tool_calls: tuple[TraceRecord, ...] = ()
    permissions: tuple[TraceRecord, ...] = ()

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the tools invoked in this step, in call order."""
        return tuple(str(record.data.get("tool", "")) for record in self.tool_calls)


@dataclass(frozen=True, slots=True)
class TraceRun:
    """A complete trace file with indexed accessors."""

    path: Path
    records: tuple[TraceRecord, ...]

    @classmethod
    def from_file(cls, path: Path, *, run_id: str | None = None) -> TraceRun:
        """Validate the trace and select its primary run or an explicit child run."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"cannot read trace file: {path}"
            raise TraceError(msg) from exc
        records: list[TraceRecord] = []
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = _RecordInput.model_validate_json(stripped)
            except ValidationError as exc:
                msg = f"invalid trace record at {path}:{number}"
                raise TraceError(msg) from exc
            records.append(
                TraceRecord(
                    name=parsed.name,
                    run_id=parsed.run_id,
                    step_id=parsed.step_id,
                    data=parsed.data,
                    occurred_at=parsed.occurred_at,
                    schema_version=parsed.schema_version,
                )
            )
        trace = cls(path, tuple(records))
        if not records:
            return trace
        selected = run_id or next(
            (
                record.run_id
                for record in records
                if record.name == "run_started" and "parent_run_id" not in record.data
            ),
            records[0].run_id,
        )
        return trace.for_run(selected)

    def for_run(self, run_id: str) -> TraceRun:
        """Derive a run-specific view without mixing steps or terminal states."""
        selected = tuple(record for record in self.records if record.run_id == run_id)
        if not selected:
            raise TraceError(f"run not found in trace: {run_id}")
        return type(self)(self.path, selected)

    @classmethod
    def from_records(
        cls, records: tuple[TraceRecord, ...], *, path: Path | None = None
    ) -> TraceRun:
        """Build a trace view from in-memory records, for tests and embedding."""
        return cls(path or Path("<memory>"), records)

    def by_name(self, name: str) -> tuple[TraceRecord, ...]:
        """Return every record with the given event name."""
        return tuple(record for record in self.records if record.name == name)

    def named(self, name: str) -> TraceRecord | None:
        """Return the first record with the given event name."""
        for record in self.records:
            if record.name == name:
                return record
        return None

    def final(self) -> TraceRecord | None:
        """Return the terminal ``run_finished`` record, if the run completed."""
        return self.named("run_finished")

    def status(self) -> str:
        """Return the terminal status, or ``unknown`` when the trace is truncated."""
        final = self.final()
        if final is None:
            return "unknown"
        return str(final.data.get("status", "unknown"))

    def failure_reason(self) -> str | None:
        """Return the terminal failure reason recorded by the loop."""
        final = self.final()
        if final is None:
            return None
        reason = final.data.get("failure_reason")
        return reason if isinstance(reason, str) and reason else None

    def steps(self) -> tuple[TraceStep, ...]:
        """Group records into ordered steps."""
        ordered = sorted(
            (record for record in self.records if record.step_id > 0),
            key=lambda record: (record.step_id, self.records.index(record)),
        )
        grouped: dict[int, list[TraceRecord]] = {}
        for record in ordered:
            grouped.setdefault(record.step_id, []).append(record)
        return tuple(self._build_step(step_id, items) for step_id, items in sorted(grouped.items()))

    @staticmethod
    def _build_step(step_id: int, items: list[TraceRecord]) -> TraceStep:
        context: TraceRecord | None = None
        model_call: TraceRecord | None = None
        tool_calls: list[TraceRecord] = []
        permissions: list[TraceRecord] = []
        for record in items:
            if record.name == "context_built":
                context = record
            elif record.name in {"model_call_completed", "model_call_failed"}:
                model_call = record
            elif record.name == "tool_call_completed":
                tool_calls.append(record)
            elif record.name == "permission_decided":
                permissions.append(record)
        return TraceStep(step_id, context, model_call, tuple(tool_calls), tuple(permissions))

    def total_tokens(self) -> tuple[int, int, int]:
        """Return summed input, output, and cached-input tokens over model calls."""
        final = self.final()
        if final is not None and "team_steps" in final.data:
            return (
                _as_int(final.data.get("input_tokens")),
                _as_int(final.data.get("output_tokens")),
                _as_int(final.data.get("cached_input_tokens")),
            )
        input_tokens = output_tokens = cached = 0
        for record in self.records:
            if record.name not in {"model_call_completed", "model_call_failed"}:
                continue
            input_tokens += _as_int(record.data.get("input_tokens"))
            output_tokens += _as_int(record.data.get("output_tokens"))
            cached += _as_int(record.data.get("cached_input_tokens"))
        return input_tokens, output_tokens, cached

    def cost_usd(self) -> float:
        """Return the summed model spend recorded in the trace."""
        final = self.final()
        if final is not None and "team_steps" in final.data:
            return _as_float(final.data.get("cost_usd"))
        return sum(
            _as_float(record.data.get("cost_usd"))
            for record in self.records
            if record.name in {"model_call_completed", "model_call_failed"}
        )

    def tool_call_count(self) -> int:
        """Return the number of completed tool calls."""
        final = self.final()
        if final is not None and "team_steps" in final.data:
            return _as_int(final.data.get("tool_calls"))
        return len(self.by_name("tool_call_completed"))

    def tool_names(self) -> tuple[str, ...]:
        """Return every completed tool name in call order."""
        return tuple(
            str(record.data.get("tool", "")) for record in self.by_name("tool_call_completed")
        )


class _RecordInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1.0"
    name: str = Field(min_length=1)
    run_id: str = ""
    step_id: int = 0
    occurred_at: str = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)


def as_int(value: JsonValue, default: int = 0) -> int:
    """Coerce a JSON value to an int without accepting booleans."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def as_float(value: JsonValue, default: float = 0.0) -> float:
    """Coerce a JSON value to a float without accepting booleans."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def as_str(value: JsonValue, default: str = "") -> str:
    """Coerce a JSON value to a string."""
    return value if isinstance(value, str) else default


_as_int = as_int
_as_float = as_float
_as_str = as_str


__all__ = [
    "TraceError",
    "TraceRecord",
    "TraceRun",
    "TraceStep",
    "as_float",
    "as_int",
    "as_str",
]

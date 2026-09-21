"""Attribute end-to-end latency to the model, the tools, and the harness itself.

A single "average latency" number for an agent run is nearly meaningless: on a real
provider, 95%+ of it is the network round-trip, which the harness author cannot
change. Splitting it out is what makes the residual — the harness overhead — a
number worth optimizing and worth quoting.
"""

from __future__ import annotations

from dataclasses import dataclass

from minidsh.domain import JsonValue, LatencyBreakdown
from minidsh.evaluation.tracefile import TraceRun, as_float, as_str


@dataclass(frozen=True, slots=True)
class ToolLatency:
    """Aggregated latency for one tool name."""

    tool: str
    calls: int
    total_ms: float

    @property
    def mean_ms(self) -> float:
        """Return the mean duration of one call."""
        return self.total_ms / self.calls if self.calls else 0.0


def latency_from_trace(trace: TraceRun) -> LatencyBreakdown:
    """Compute the three-layer latency split from a completed trace."""
    model_ms = 0.0
    model_calls = 0
    for record in trace.by_name("model_call_completed"):
        model_ms += as_float(record.data.get("duration_ms"))
        model_calls += 1
    tool_ms = 0.0
    tool_calls = 0
    for record in trace.by_name("tool_call_completed"):
        tool_ms += as_float(record.data.get("duration_ms"))
        tool_calls += 1
    final = trace.final()
    total_ms = as_float(final.data.get("duration_ms")) if final is not None else model_ms + tool_ms
    observed = model_ms + tool_ms
    return LatencyBreakdown(
        model_ms=model_ms,
        tool_ms=tool_ms,
        harness_ms=max(0.0, total_ms - observed),
        total_ms=total_ms,
        model_calls=model_calls,
        tool_calls=tool_calls,
    )


def tool_latencies(trace: TraceRun) -> tuple[ToolLatency, ...]:
    """Aggregate tool durations by tool name, slowest first."""
    totals: dict[str, list[float]] = {}
    for record in trace.by_name("tool_call_completed"):
        name = as_str(record.data.get("tool"), "unknown")
        bucket = totals.setdefault(name, [0.0, 0])
        bucket[0] += as_float(record.data.get("duration_ms"))
        bucket[1] += 1
    aggregates = [ToolLatency(tool, int(values[1]), values[0]) for tool, values in totals.items()]
    return tuple(sorted(aggregates, key=lambda item: (-item.total_ms, item.tool)))


def retried_tool_calls(trace: TraceRun) -> int:
    """Return the number of tool calls that needed more than one attempt."""
    return sum(
        1
        for record in trace.by_name("tool_call_completed")
        if isinstance(record.data.get("attempt"), int) and as_float(record.data.get("attempt")) > 1
    )


def to_json(breakdown: LatencyBreakdown, trace: TraceRun | None = None) -> dict[str, JsonValue]:
    """Return the breakdown plus optional per-tool detail for a report."""
    payload: dict[str, JsonValue] = dict(breakdown.to_json())
    if trace is not None:
        payload["tools"] = [
            {"tool": item.tool, "calls": item.calls, "total_ms": round(item.total_ms, 3)}
            for item in tool_latencies(trace)
        ]
        payload["retried_tool_calls"] = retried_tool_calls(trace)
    return payload


__all__ = ["ToolLatency", "latency_from_trace", "retried_tool_calls", "to_json", "tool_latencies"]

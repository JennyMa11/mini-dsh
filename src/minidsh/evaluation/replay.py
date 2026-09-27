"""Replay a run from its JSONL trace without calling a model again.

Replay exists because the trace, not the transcript, is the fact source. Given only
the trace file you can reconstruct the full ``Context → Model → Tool Call →
Observation → State`` sequence, which is what failure analysis actually needs: you
want to see the context the model was handed at the step where it went wrong, and
that information is not recoverable from a final answer or a diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from minidsh.domain import JsonValue, LatencyBreakdown
from minidsh.evaluation.latency import latency_from_trace
from minidsh.evaluation.tracefile import TraceRun, TraceStep, as_float, as_int, as_str

_CONTEXT_ARROW = "Context"
_MODEL_ARROW = "Model"
_TOOL_ARROW = "Tool Call"
_OBSERVATION_ARROW = "Observation"
_STATE_ARROW = "State"


@dataclass(frozen=True, slots=True)
class ReplayToolCall:
    """One tool invocation as it appeared at runtime."""

    tool: str
    status: str
    duration_ms: float
    truncated: bool
    attempt: int
    error_code: str
    preview: str


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One fully reconstructed agent step."""

    step_id: int
    state: str
    context_strategy: str
    context_tokens: int
    omitted_messages: int
    model_finish_reason: str
    model_content: str
    model_ms: float
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    tool_calls: tuple[ReplayToolCall, ...]


@dataclass(frozen=True, slots=True)
class Replay:
    """A complete, model-free reconstruction of one run."""

    run_id: str
    status: str
    failure_reason: str | None
    steps: tuple[ReplayStep, ...]
    latency: LatencyBreakdown
    cost_usd: float
    team_tokens: tuple[int, int, int] | None = None

    @property
    def tokens(self) -> tuple[int, int, int]:
        """Return summed input, output, and cached-input tokens."""
        if self.team_tokens is not None:
            return self.team_tokens
        input_tokens = sum(step.input_tokens for step in self.steps)
        output_tokens = sum(step.output_tokens for step in self.steps)
        cached = sum(step.cached_input_tokens for step in self.steps)
        return input_tokens, output_tokens, cached

    def to_json(self) -> dict[str, JsonValue]:
        """Return a stable serialization for reports and tests."""
        input_tokens, output_tokens, cached = self.tokens
        return {
            "run_id": self.run_id,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "steps": len(self.steps),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached,
            "cost_usd": self.cost_usd,
            "latency": self.latency.to_json(),
        }


def build_replay(trace: TraceRun) -> Replay:
    """Reconstruct every step of a run from its trace records."""
    run_id = trace.records[0].run_id if trace.records else ""
    final = trace.final()
    return Replay(
        run_id=run_id,
        status=trace.status(),
        failure_reason=trace.failure_reason(),
        steps=tuple(_replay_step(step) for step in trace.steps()),
        latency=latency_from_trace(trace),
        cost_usd=trace.cost_usd(),
        team_tokens=(
            trace.total_tokens() if final is not None and "team_steps" in final.data else None
        ),
    )


def _replay_step(step: TraceStep) -> ReplayStep:
    context = step.context
    model = step.model_call
    return ReplayStep(
        step_id=step.step_id,
        state=_state_for(step),
        context_strategy=as_str(context.data.get("strategy"), "-") if context else "-",
        context_tokens=as_int(context.data.get("estimated_tokens")) if context else 0,
        omitted_messages=as_int(context.data.get("omitted_messages")) if context else 0,
        model_finish_reason=as_str(model.data.get("finish_reason"), "-") if model else "-",
        model_content=as_str(model.data.get("content")) if model else "",
        model_ms=as_float(model.data.get("duration_ms")) if model else 0.0,
        input_tokens=as_int(model.data.get("input_tokens")) if model else 0,
        output_tokens=as_int(model.data.get("output_tokens")) if model else 0,
        cached_input_tokens=as_int(model.data.get("cached_input_tokens")) if model else 0,
        tool_calls=tuple(_tool_call(record.data) for record in step.tool_calls),
    )


def _state_for(step: TraceStep) -> str:
    """Return the loop state implied by a step's own facts."""
    if step.model_call is None:
        return "incomplete"
    if step.model_call.name == "model_call_failed":
        return "failed"
    if step.tool_calls:
        return "continue"
    return "finish"


def _tool_call(data: dict[str, JsonValue]) -> ReplayToolCall:
    error = data.get("error")
    code = as_str(error.get("code")) if isinstance(error, dict) else ""
    return ReplayToolCall(
        tool=as_str(data.get("tool"), "unknown"),
        status=as_str(data.get("status"), "unknown"),
        duration_ms=as_float(data.get("duration_ms")),
        truncated=data.get("truncated") is True,
        attempt=as_int(data.get("attempt"), 1),
        error_code=code,
        preview=_preview(data.get("result")),
    )


def _preview(value: JsonValue, *, limit: int = 160) -> str:
    if value is None:
        return ""
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return rendered if len(rendered) <= limit else f"{rendered[:limit]}…"


def to_markdown(replay: Replay, *, preview_chars: int = 120) -> str:
    """Render a step-by-step replay that mirrors the loop's own vocabulary."""
    input_tokens, output_tokens, cached = replay.tokens
    lines = [
        f"# Trace replay: `{replay.run_id}`",
        "",
        f"- Status: `{replay.status}`",
        f"- Failure reason: {replay.failure_reason or '-'}",
        f"- Steps: {len(replay.steps)}",
        f"- Tokens: {input_tokens} in / {output_tokens} out ({cached} cached)",
        f"- Cost: ${replay.cost_usd:.6f}",
        f"- Latency: {replay.latency.total_ms:.1f} ms total "
        f"({replay.latency.model_ms:.1f} model, {replay.latency.tool_ms:.1f} tool, "
        f"{replay.latency.harness_ms:.1f} harness)",
        "",
    ]
    for step in replay.steps:
        lines += [
            f"## Step {step.step_id} → `{step.state}`",
            "",
            f"1. **{_CONTEXT_ARROW}** strategy=`{step.context_strategy}` "
            f"tokens≈{step.context_tokens} omitted={step.omitted_messages}",
            f"2. **{_MODEL_ARROW}** finish=`{step.model_finish_reason}` "
            f"in={step.input_tokens} out={step.output_tokens} "
            f"cached={step.cached_input_tokens} {step.model_ms:.1f} ms",
        ]
        if step.model_content:
            lines.append(f"   > {_clip(step.model_content, preview_chars)}")
        if not step.tool_calls:
            lines.append(f"3. **{_TOOL_ARROW}** none — loop terminated")
        for call in step.tool_calls:
            retry = f" attempt={call.attempt}" if call.attempt > 1 else ""
            truncation = " truncated" if call.truncated else ""
            error = f" error={call.error_code}" if call.error_code else ""
            lines += [
                f"3. **{_TOOL_ARROW}** `{call.tool}` → `{call.status}`"
                f" {call.duration_ms:.1f} ms{retry}{truncation}{error}",
                f"4. **{_OBSERVATION_ARROW}** {_clip(call.preview, preview_chars) or '(empty)'}",
            ]
        lines += ["", f"**{_STATE_ARROW}** `{step.state}`", ""]
    return "\n".join(lines) + "\n"


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"


__all__ = ["Replay", "ReplayStep", "ReplayToolCall", "build_replay", "to_markdown"]

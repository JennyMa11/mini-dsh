"""Diff two traces to find *where* two harness variants stopped being comparable.

Comparing two ablation variants by their aggregate tokens tells you a number changed.
It does not tell you why. Because both runs share a task manifest, their traces are
aligned step by step, so the first step where the two runs issue different tool calls
is the step that caused every downstream difference — and that is the only step worth
reading.
"""

from __future__ import annotations

from dataclasses import dataclass

from minidsh.domain import JsonValue
from minidsh.evaluation.latency import latency_from_trace
from minidsh.evaluation.tracefile import TraceRecord, TraceRun, TraceStep, as_int, as_str


@dataclass(frozen=True, slots=True)
class StepComparison:
    """One aligned step from both runs."""

    step_id: int
    left_summary: str
    right_summary: str
    context_delta: int
    token_delta: int
    divergence: str | None = None

    @property
    def diverged(self) -> bool:
        """Return whether the two runs behaved differently at this step."""
        return self.divergence is not None


@dataclass(frozen=True, slots=True)
class TraceDiff:
    """Aligned, step-by-step comparison of two runs of the same task."""

    left_label: str
    right_label: str
    left_status: str
    right_status: str
    left_tokens: int
    right_tokens: int
    left_cached: int
    right_cached: int
    left_cost_usd: float
    right_cost_usd: float
    left_model_ms: float
    right_model_ms: float
    divergence_step: int | None
    divergence_kind: str | None
    steps: tuple[StepComparison, ...]

    @property
    def token_delta(self) -> int:
        """Return right-minus-left total tokens."""
        return self.right_tokens - self.left_tokens

    @property
    def token_delta_pct(self) -> float:
        """Return the relative token change, or 0 when the left run used no tokens."""
        if self.left_tokens == 0:
            return 0.0
        return self.token_delta / self.left_tokens

    @property
    def cost_delta_usd(self) -> float:
        """Return right-minus-left total cost."""
        return self.right_cost_usd - self.left_cost_usd

    def to_json(self) -> dict[str, JsonValue]:
        """Return a stable serialization for reports and tests."""
        return {
            "left": self.left_label,
            "right": self.right_label,
            "left_status": self.left_status,
            "right_status": self.right_status,
            "left_tokens": self.left_tokens,
            "right_tokens": self.right_tokens,
            "token_delta": self.token_delta,
            "token_delta_pct": round(self.token_delta_pct, 4),
            "left_cached_input_tokens": self.left_cached,
            "right_cached_input_tokens": self.right_cached,
            "left_cost_usd": self.left_cost_usd,
            "right_cost_usd": self.right_cost_usd,
            "divergence_step": self.divergence_step,
            "divergence_kind": self.divergence_kind,
            "steps": len(self.steps),
        }


def diff_traces(
    left: TraceRun,
    right: TraceRun,
    *,
    left_label: str = "left",
    right_label: str = "right",
) -> TraceDiff:
    """Align two traces by step id and locate the first behavioural divergence."""
    left_steps = {step.step_id: step for step in left.steps()}
    right_steps = {step.step_id: step for step in right.steps()}
    comparisons: list[StepComparison] = []
    divergence_step: int | None = None
    divergence_kind: str | None = None
    for step_id in sorted(set(left_steps) | set(right_steps)):
        comparison = _compare(step_id, left_steps.get(step_id), right_steps.get(step_id))
        comparisons.append(comparison)
        if comparison.diverged and divergence_step is None:
            divergence_step = step_id
            divergence_kind = comparison.divergence
    left_input, left_output, left_cached = left.total_tokens()
    right_input, right_output, right_cached = right.total_tokens()
    return TraceDiff(
        left_label=left_label,
        right_label=right_label,
        left_status=left.status(),
        right_status=right.status(),
        left_tokens=left_input + left_output,
        right_tokens=right_input + right_output,
        left_cached=left_cached,
        right_cached=right_cached,
        left_cost_usd=left.cost_usd(),
        right_cost_usd=right.cost_usd(),
        left_model_ms=latency_from_trace(left).model_ms,
        right_model_ms=latency_from_trace(right).model_ms,
        divergence_step=divergence_step,
        divergence_kind=divergence_kind,
        steps=tuple(comparisons),
    )


def _compare(step_id: int, left: TraceStep | None, right: TraceStep | None) -> StepComparison:
    if left is None or right is None:
        missing = "right" if left is None else "left"
        return StepComparison(
            step_id,
            _summarize(left),
            _summarize(right),
            0,
            0,
            divergence=f"step present only in the {missing} run",
        )
    context_delta = as_int(_value(right.context, "estimated_tokens")) - as_int(
        _value(left.context, "estimated_tokens")
    )
    token_delta = (
        as_int(_value(right.model_call, "input_tokens"))
        + as_int(_value(right.model_call, "output_tokens"))
        - as_int(_value(left.model_call, "input_tokens"))
        - as_int(_value(left.model_call, "output_tokens"))
    )
    divergence = _divergence(left, right)
    return StepComparison(
        step_id,
        _summarize(left),
        _summarize(right),
        context_delta,
        token_delta,
        divergence,
    )


def _divergence(left: TraceStep, right: TraceStep) -> str | None:
    if left.tool_names != right.tool_names:
        return (
            f"tool calls diverged: {left.tool_names or '(none)'} vs {right.tool_names or '(none)'}"
        )
    left_finish = as_str(_value(left.model_call, "finish_reason"), "-")
    right_finish = as_str(_value(right.model_call, "finish_reason"), "-")
    if left_finish != right_finish:
        return f"finish reason diverged: {left_finish} vs {right_finish}"
    left_strategy = as_str(_value(left.context, "strategy"), "-")
    right_strategy = as_str(_value(right.context, "strategy"), "-")
    if left_strategy != right_strategy:
        return f"context strategy diverged: {left_strategy} vs {right_strategy}"
    return None


def _value(record: TraceRecord | None, key: str) -> JsonValue:
    """Return one field of a trace record, or ``None`` when the record is absent."""
    if record is None:
        return None
    return record.data.get(key)


def _summarize(step: TraceStep | None) -> str:
    if step is None:
        return "(absent)"
    strategy = as_str(_value(step.context, "strategy"), "-")
    tools = ",".join(step.tool_names) or "-"
    tokens = as_int(_value(step.model_call, "input_tokens")) + as_int(
        _value(step.model_call, "output_tokens")
    )
    return f"context={strategy} tokens={tokens} tools={tools}"


def to_markdown(diff: TraceDiff) -> str:
    """Render the diff as a readable alignment report."""
    lines = [
        f"# Trace diff: `{diff.left_label}` vs `{diff.right_label}`",
        "",
        f"- Status: `{diff.left_status}` vs `{diff.right_status}`",
        f"- Tokens: {diff.left_tokens} vs {diff.right_tokens} "
        f"({diff.token_delta:+d}, {diff.token_delta_pct:+.1%})",
        f"- Cached input tokens: {diff.left_cached} vs {diff.right_cached}",
        f"- Cost: ${diff.left_cost_usd:.6f} vs ${diff.right_cost_usd:.6f} "
        f"({diff.cost_delta_usd:+.6f})",
        f"- Model latency: {diff.left_model_ms:.1f} ms vs {diff.right_model_ms:.1f} ms",
        "",
    ]
    if diff.divergence_step is None:
        lines.append("The two runs are step-for-step equivalent: no behavioural divergence.")
    else:
        lines.append(
            f"First behavioural divergence at **step {diff.divergence_step}**: "
            f"{diff.divergence_kind}"
        )
    lines += [
        "",
        "| Step | Context Δ | Token Δ | Left | Right | Divergence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {step.step_id} | {step.context_delta:+d} | {step.token_delta:+d} | "
        f"{step.left_summary} | {step.right_summary} | {step.divergence or '-'} |"
        for step in diff.steps
    ]
    return "\n".join(lines) + "\n"


__all__ = ["StepComparison", "TraceDiff", "diff_traces", "to_markdown"]

"""Evidence-aware component, trajectory and outcome metrics over saved traces.

No model calls, mutation, or automatic judgments of intent occur here. Semantic
scores require independent, run-bound checks; missing evidence remains unknown.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from minidsh.adapters.scoring_evidence import Checks
from minidsh.adapters.scoring_evidence import (
    ScoringBudget as ScoringBudget,  # noqa: PLC0414 - public export
)
from minidsh.adapters.scoring_evidence import (
    ScoringEvidence as ScoringEvidence,  # noqa: PLC0414 - public export
)
from minidsh.domain import JsonValue
from minidsh.evaluation.tracefile import TraceRecord, TraceRun

_EXECUTED = frozenset({"succeeded", "failed", "timed_out"})
_STATUSES = _EXECUTED | {"invalid", "denied"}


@dataclass(frozen=True, slots=True)
class MetricRate:
    """A rate with its observed denominator and declared opportunities."""

    numerator: int
    denominator: int
    eligible: int

    @property
    def value(self) -> float | None:
        """Return None when there is no measured opportunity."""
        return self.numerator / self.denominator if self.denominator else None

    @property
    def coverage(self) -> float | None:
        """Return how much of the declared evidence has been checked."""
        return self.denominator / self.eligible if self.eligible else None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize sufficient statistics rather than a bare percentage."""
        return {
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "eligible": self.eligible,
            "coverage": self.coverage,
        }


@dataclass(frozen=True, slots=True)
class HarnessMetrics:
    """Regenerable scores; semantic annotations retain their provenance."""

    run_id: str
    case_id: str | None
    evidence_source: str | None
    measurements: dict[str, JsonValue]
    rates: dict[str, MetricRate]

    def to_json(self) -> dict[str, JsonValue]:
        """Return a versioned report independent of historical benchmark schemas."""
        return {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "case_id": self.case_id,
            "evidence_source": self.evidence_source,
            "measurements": self.measurements,
            "rates": {name: rate.to_json() for name, rate in self.rates.items()},
        }


def _rate(checks: Mapping[str, bool | None], *, inverted: bool = False) -> MetricRate:
    known = [item for item in checks.values() if item is not None]
    return MetricRate(sum(item != inverted for item in known), len(known), len(checks))


def _number(value: JsonValue) -> int | float | None:
    if (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    ):
        return value
    return None


def _integer(value: JsonValue) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _tool_error(record: TraceRecord) -> bool:
    if _status(record) in {"failed", "timed_out"}:
        return True
    result = record.data.get("result")
    return (
        isinstance(result, dict)
        and type(result.get("exit_code")) is int
        and result["exit_code"] != 0
    )


def _status(record: TraceRecord) -> str:
    value = record.data.get("status")
    return value if isinstance(value, str) else "unknown"


def _repetition(trace: TraceRun, calls: tuple[TraceRecord, ...]) -> MetricRate:
    arguments: dict[str, JsonValue] = {}
    for record in trace.records:
        if record.name == "tool_call_started":
            arguments[str(record.data.get("call_id"))] = record.data.get("arguments")
        elif record.name == "model_call_completed":
            proposed = record.data.get("tool_calls")
            if isinstance(proposed, list):
                for proposal in proposed:
                    if isinstance(proposal, dict):
                        arguments[str(proposal.get("call_id"))] = proposal.get("arguments")
    previous: str | None = None
    repeats = measured = 0
    for call in calls:
        args = arguments.get(str(call.data.get("call_id")))
        if args is None or _status(call) not in _STATUSES:
            previous = None
            continue
        measured += 1
        if not _tool_error(call) and _status(call) not in {"invalid", "denied"}:
            previous = None
            continue
        error = call.data.get("error")
        result = call.data.get("result")
        signature = json.dumps(
            [
                call.data.get("tool"),
                args,
                call.data.get("status"),
                error.get("code") if isinstance(error, dict) else None,
                result.get("exit_code") if isinstance(result, dict) else None,
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        repeats += signature == previous
        previous = signature
    return MetricRate(repeats, measured, len(calls))


def _usage(trace: TraceRun) -> tuple[dict[str, JsonValue], dict[str, MetricRate]]:
    calls = tuple(
        record
        for record in trace.records
        if record.name in {"model_call_completed", "model_call_failed"}
    )
    unmatched = max(0, len(trace.by_name("model_call_started")) - len(calls))
    unknown = unmatched
    input_tokens = output_tokens = cached = cache_exact = cache_input = cache_calls = hits = 0
    for call in calls:
        tokens_in = _integer(call.data.get("input_tokens"))
        tokens_out = _integer(call.data.get("output_tokens"))
        incomplete = (
            call.data.get("usage_known") is False or tokens_in is None or tokens_out is None
        )
        unknown += incomplete
        input_tokens += tokens_in or 0
        output_tokens += tokens_out or 0
        hit = _integer(call.data.get("cached_input_tokens"))
        if hit is not None and tokens_in is not None and hit <= tokens_in:
            cached += hit
            if not incomplete:
                cache_exact += hit
                cache_input += tokens_in
                cache_calls += 1
                hits += hit > 0
    final = trace.final()
    if final is not None:
        unknown = max(unknown, _integer(final.data.get("unknown_usage_calls")) or 0)
    return (
        {
            "total_tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached,
            "unknown_usage_calls": unknown,
            "token_usage_complete": unknown == 0 and trace.final() is not None,
            "model_calls": len(calls) + unmatched,
        },
        {
            "cached_input_share": MetricRate(cache_exact, cache_input, input_tokens),
            "cache_hit_rate": MetricRate(hits, cache_calls, len(calls) + unmatched),
        },
    )


def _constraints(
    trace: TraceRun,
    evidence: ScoringEvidence | None,
    measurements: dict[str, JsonValue],
) -> Checks:
    checks: Checks = dict(evidence.constraint_checks) if evidence else {}
    budget = evidence.budget if evidence else None
    if budget is None:
        return checks
    limits = {
        "steps": budget.max_steps,
        "tokens": budget.max_tokens,
        "tool_calls": budget.max_tool_calls,
        "duration_ms": budget.max_duration_seconds * 1000
        if budget.max_duration_seconds is not None
        else None,
    }
    final = trace.final()
    for name, limit in limits.items():
        if limit is None:
            continue
        key = "budget." + name
        if key in checks:
            raise ValueError("duplicate constraint name reserved for budget")
        value = measurements.get("total_tokens" if name == "tokens" else name)
        observed = _number(value)
        complete = final is not None and (
            name != "tokens" or measurements["token_usage_complete"] is True
        )
        checks[key] = (
            False
            if observed is not None and observed > limit
            else observed <= limit
            if observed is not None and complete
            else None
        )
        if name == "tokens":
            measurements["token_overrun"] = (
                max(0, observed - limit)
                if observed is not None and (complete or observed > limit)
                else None
            )
    return checks


def score_harness(trace: TraceRun, *, evidence: ScoringEvidence | None = None) -> HarnessMetrics:
    """Score one run without treating terminal status as independently verified success."""
    run_ids = {record.run_id for record in trace.records}
    if len(run_ids) != 1:
        raise ValueError("harness scoring requires exactly one run_id")
    run_id = next(iter(run_ids))
    if evidence is not None and evidence.run_id != run_id:
        raise ValueError("evidence run_id does not match trace")
    measurements, rates = _usage(trace)
    final = trace.final()
    measurements.update(
        {
            "finalized": final is not None,
            "status": trace.status(),
            "steps": _integer(final.data.get("steps")) if final else None,
            "tool_calls": _integer(final.data.get("tool_calls"))
            if final and _integer(final.data.get("tool_calls")) is not None
            else len(trace.by_name("tool_call_completed")),
            "duration_ms": _number(final.data.get("duration_ms")) if final else None,
        }
    )
    calls = trace.by_name("tool_call_completed")
    known = [call for call in calls if _status(call) in _STATUSES]
    executed = [call for call in known if _status(call) in _EXECUTED]
    rates.update(
        {
            "tool_error_rate": MetricRate(
                sum(_tool_error(call) for call in executed), len(executed), len(executed)
            ),
            "invalid_tool_call_rate": MetricRate(
                sum(call.data.get("status") == "invalid" for call in known), len(known), len(calls)
            ),
            "permission_denied_rate": MetricRate(
                sum(call.data.get("status") == "denied" for call in known), len(known), len(calls)
            ),
            "repeated_failed_action_rate": _repetition(trace, calls),
        }
    )
    retried = [call for call in calls if (_integer(call.data.get("attempt")) or 0) > 1]
    rates["retry_recovery_rate"] = MetricRate(
        sum(call.data.get("status") == "succeeded" and not _tool_error(call) for call in retried),
        len(retried),
        len(retried),
    )
    constraints = _constraints(trace, evidence, measurements)
    rates["constraint_satisfaction"] = _rate(constraints)
    compliant = (
        False
        if any(value is False for value in constraints.values())
        else True
        if constraints and all(value is True for value in constraints.values())
        else None
    )
    rates["constraint_satisfied_run_rate"] = _rate({"task": compliant})
    measurements["constraint_checks"] = dict(constraints)
    goals = evidence.goal_checks if evidence else {}
    partial = (
        0 < sum(value is True for value in goals.values()) < len(goals)
        if goals and all(value is not None for value in goals.values())
        else None
    )
    rates["partial_success_rate"] = _rate({"task": partial})
    rates["goal_completion_rate"] = _rate(goals)
    rates["task_success_rate"] = _rate({"task": evidence.task_success if evidence else None})
    rates["user_acceptance_rate"] = _rate({"task": evidence.user_accepted if evidence else None})
    rates["human_escalation_rate"] = _rate({"task": evidence.human_escalated if evidence else None})
    for name, checks, inverted in (
        ("tool_call_accuracy", evidence.tool_selection_checks if evidence else {}, False),
        ("recovery_rate", evidence.recovery_checks if evidence else {}, False),
        ("unsafe_action_block_rate", evidence.unsafe_action_checks if evidence else {}, False),
        ("safe_action_false_block_rate", evidence.safe_action_checks if evidence else {}, True),
        ("secret_leak_rate", evidence.secret_checks if evidence else {}, True),
        ("policy_violation_rate", evidence.policy_checks if evidence else {}, True),
    ):
        rates[name] = _rate(checks, inverted=inverted)
    return HarnessMetrics(
        run_id,
        evidence.case_id if evidence else None,
        evidence.source if evidence else None,
        measurements,
        rates,
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate_harness_metrics(scores: Sequence[HarnessMetrics]) -> dict[str, JsonValue]:
    """Pool measured opportunities, never average percentages with unequal denominators."""
    rates: dict[str, JsonValue] = {}
    for name in sorted({name for score in scores for name in score.rates}):
        items = [score.rates[name] for score in scores if name in score.rates]
        rates[name] = MetricRate(
            sum(item.numerator for item in items),
            sum(item.denominator for item in items),
            sum(item.eligible for item in items),
        ).to_json()
    measurements: dict[str, JsonValue] = {"runs": len(scores)}
    for name in (
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "unknown_usage_calls",
        "model_calls",
        "tool_calls",
    ):
        measurements[name] = sum(_integer(score.measurements.get(name)) or 0 for score in scores)
    measurements["token_usage_complete"] = (
        all(score.measurements.get("token_usage_complete") is True for score in scores)
        if scores
        else None
    )
    for name in ("steps", "tool_calls"):
        values = [
            float(value)
            for score in scores
            if (value := _number(score.measurements.get(name))) is not None
        ]
        measurements["avg_" + name] = sum(values) / len(values) if values else None
        measurements[name + "_sample_count"] = len(values)
    durations = [
        float(value)
        for score in scores
        if (value := _number(score.measurements.get("duration_ms"))) is not None
    ]
    measurements.update(
        {
            "latency_p50_ms": _percentile(durations, 0.5),
            "latency_p95_ms": _percentile(durations, 0.95),
            "latency_sample_count": len(durations),
            "finalized_runs": sum(score.measurements.get("finalized") is True for score in scores),
        }
    )
    return {"schema_version": "1.0", "measurements": measurements, "rates": rates}

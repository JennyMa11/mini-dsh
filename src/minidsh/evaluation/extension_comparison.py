"""Read-only, predeclared paired analysis of the fixed extension-v1 cohort."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from minidsh.domain import JsonValue
from minidsh.evaluation.harness_metrics import MetricRate

GROUPS = ("Baseline", "A", "B", "C", "D")
PAIRS = (("Baseline", "A"), ("A", "B"), ("B", "C"), ("C", "D"), ("Baseline", "D"))
RESAMPLES = 10000
SEED = 20261029
CASE_COUNT = 6
REPEATS = 3
RUNS_PER_GROUP = CASE_COUNT * REPEATS


@dataclass(frozen=True, slots=True)
class ComparisonRun:
    """One declared slot; tokens may be a known lower bound when usage is unknown."""

    case_id: str
    repeat: int
    group: str
    task_success: bool | None = None
    tokens: int | None = None
    usage_complete: bool = False
    finalized: bool = False
    latency_ms: float | None = None
    memory_match: bool | None = None
    memory_exposed: bool | None = None
    observed: bool = True

    def __post_init__(self) -> None:
        if (
            not self.case_id
            or self.group not in GROUPS
            or type(self.repeat) is not int
            or self.repeat not in range(REPEATS)
            or (self.tokens is not None and (type(self.tokens) is not int or self.tokens < 0))
            or (
                self.latency_ms is not None
                and (
                    isinstance(self.latency_ms, bool)
                    or not math.isfinite(self.latency_ms)
                    or self.latency_ms < 0
                )
            )
            or any(
                value is not None and type(value) is not bool
                for value in (self.task_success, self.memory_match, self.memory_exposed)
            )
            or any(
                type(value) is not bool
                for value in (self.usage_complete, self.finalized, self.observed)
            )
        ):
            raise ValueError("invalid_extension_comparison_slot")


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """Complete-cohort quantities remain unknown until every declared slot is checked."""

    observed_runs: int
    success_rate: MetricRate
    known_tokens: int
    total_tokens: int | None
    usage_complete_runs: int
    token_ratio_checked_runs: int
    tokens_per_success: float | None
    latency_mean_ms: float | None
    latency_sample_count: int
    memory_selector_match: MetricRate
    memory_model_exposure: MetricRate

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "declared_runs": RUNS_PER_GROUP,
            "observed_runs": self.observed_runs,
            "success_rate": self.success_rate.to_json(),
            "known_tokens": self.known_tokens,
            "total_tokens": self.total_tokens,
            "usage_complete_runs": self.usage_complete_runs,
            "tokens_per_success": self.tokens_per_success,
            "tokens_per_success_evidence": {
                "numerator_tokens": self.total_tokens
                if self.token_ratio_checked_runs == RUNS_PER_GROUP
                else None,
                "denominator_successes": self.success_rate.numerator
                if self.success_rate.denominator == RUNS_PER_GROUP
                else None,
                "checked_runs": self.token_ratio_checked_runs,
                "declared_runs": RUNS_PER_GROUP,
                "coverage": self.token_ratio_checked_runs / RUNS_PER_GROUP,
            },
            "latency_mean_ms": self.latency_mean_ms,
            "latency_sample_count": self.latency_sample_count,
            "memory_selector_match": self.memory_selector_match.to_json(),
            "memory_model_exposure": self.memory_model_exposure.to_json(),
        }


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """Enhanced minus baseline; undefined ratio replicates are never discarded."""

    baseline: float | None
    enhanced: float | None
    delta: float | None
    interval_95: tuple[float, float] | None
    valid_resamples: int
    undefined_resamples: int
    reason: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "baseline": self.baseline,
            "enhanced": self.enhanced,
            "delta": self.delta,
            "interval_95": list(self.interval_95) if self.interval_95 is not None else None,
            "valid_resamples": self.valid_resamples,
            "undefined_resamples": self.undefined_resamples,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PairedComparison:
    """All eighteen paired opportunities stay declared, including missing runs."""

    baseline: str
    enhanced: str
    known_outcome_pairs: int
    complete_usage_pairs: int
    known_latency_pairs: int
    metrics: dict[str, MetricComparison]
    benefit: str
    assessment_blockers: tuple[str, ...]

    @property
    def declared_pairs(self) -> int:
        return RUNS_PER_GROUP

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "baseline": self.baseline,
            "enhanced": self.enhanced,
            "scope": "combined_effect"
            if self.baseline == "Baseline" and self.enhanced == "D"
            else "incremental_effect_with_preceding_features_enabled",
            "declared_pairs": self.declared_pairs,
            "known_outcome_pairs": self.known_outcome_pairs,
            "complete_usage_pairs": self.complete_usage_pairs,
            "known_latency_pairs": self.known_latency_pairs,
            "metrics": {key: value.to_json() for key, value in self.metrics.items()},
            "benefit": self.benefit,
            "assessment_blockers": list(self.assessment_blockers),
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Exploratory six-case report; synthetic data cannot establish mechanism benefit."""

    case_ids: tuple[str, ...]
    groups: dict[str, GroupSummary]
    comparisons: dict[str, PairedComparison]
    complete_cohort: bool
    safety_passed: bool | None
    real_model_results: bool

    @property
    def declared_runs(self) -> int:
        return 90

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": "extension_comparison_v1",
            "protocol": {
                "unit": "case_cluster_with_three_repeats",
                "case_order": list(self.case_ids),
                "resamples": RESAMPLES,
                "seed": SEED,
                "interval_level": 0.95,
                "quantile": "linear interpolation at (n-1)*p, p=.025/.975",
                "delta_direction": "enhanced_minus_baseline",
                "latency_scope": "mean run_finished.duration_ms: Loop timer start to result "
                "construction; excludes composition, finishing/end cleanup, capture and verifier",
                "complete_cohort_requires": "known outcomes and finalized known token usage "
                "in all ninety declared slots; latency and safety coverage are separate",
                "undefined_ratios": "retain counts; interval null if any replicate is undefined",
            },
            "declared_runs": self.declared_runs,
            "complete_cohort": self.complete_cohort,
            "safety_passed": self.safety_passed,
            "real_model_results": self.real_model_results,
            "groups": {key: value.to_json() for key, value in self.groups.items()},
            "comparisons": {key: value.to_json() for key, value in self.comparisons.items()},
            "scope": "six fixed known tasks; exploratory intervals, no population generalization",
        }


def _usage(row: ComparisonRun) -> bool:
    return row.finalized and row.usage_complete and row.tokens is not None


def _latency(row: ComparisonRun) -> bool:
    return row.finalized and row.latency_ms is not None


def _summary(rows: Sequence[ComparisonRun], group: str) -> GroupSummary:
    known = [r.task_success for r in rows if r.task_success is not None]
    successes = sum(known)
    tokens = sum(r.tokens for r in rows if r.tokens is not None)
    usage_count = sum(_usage(r) for r in rows)
    complete = usage_count == RUNS_PER_GROUP
    durations = [r.latency_ms for r in rows if _latency(r) and r.latency_ms is not None]
    matched = [r.memory_match for r in rows if r.memory_match is not None]
    exposed = [r.memory_exposed for r in rows if r.memory_exposed is not None]
    return GroupSummary(
        sum(r.observed for r in rows),
        MetricRate(successes, len(known), RUNS_PER_GROUP),
        tokens,
        tokens if complete else None,
        usage_count,
        sum(_usage(r) and r.task_success is not None for r in rows),
        tokens / successes if complete and len(known) == RUNS_PER_GROUP and successes else None,
        sum(durations) / RUNS_PER_GROUP if len(durations) == RUNS_PER_GROUP else None,
        len(durations),
        MetricRate(sum(matched), len(matched), RUNS_PER_GROUP if group == "D" else 0),
        MetricRate(sum(exposed), len(exposed), RUNS_PER_GROUP if group == "D" else 0),
    )


def _value(rows: Sequence[ComparisonRun], metric: str) -> float | None:
    if metric == "latency_mean_ms":
        if not all(_latency(r) for r in rows):
            return None
        return sum(r.latency_ms for r in rows if r.latency_ms is not None) / len(rows)
    if not all(r.task_success is not None for r in rows):
        return None
    successes = sum(r.task_success is True for r in rows)
    if metric == "success_rate":
        return successes / len(rows)
    if not all(_usage(r) for r in rows) or not successes:
        return None
    return sum(r.tokens for r in rows if r.tokens is not None) / successes


def _quantile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _metric(
    baseline: tuple[ComparisonRun, ...],
    enhanced: tuple[ComparisonRun, ...],
    draws: Sequence[tuple[int, ...]],
    metric: str,
    *,
    complete: bool,
) -> MetricComparison:
    a, b = _value(baseline, metric), _value(enhanced, metric)
    point = b - a if a is not None and b is not None else None
    if not complete:
        return MetricComparison(a, b, point, None, 0, 0, "missing_paired_evidence")
    deltas = []
    undefined = 0
    for draw in draws:
        left = tuple(
            baseline[index * REPEATS + repeat] for index in draw for repeat in range(REPEATS)
        )
        right = tuple(
            enhanced[index * REPEATS + repeat] for index in draw for repeat in range(REPEATS)
        )
        av, bv = _value(left, metric), _value(right, metric)
        if av is None or bv is None:
            undefined += 1
        else:
            deltas.append(bv - av)
    deltas.sort()
    interval = (_quantile(deltas, 0.025), _quantile(deltas, 0.975)) if not undefined else None
    return MetricComparison(
        a,
        b,
        point,
        interval,
        len(deltas),
        undefined,
        "resamples_without_success" if undefined else None,
    )


def compare_extension_groups(
    rows: Sequence[ComparisonRun],
    *,
    case_ids: Sequence[str],
    safety_passed: bool | None,
    real_model_results: bool,
) -> ComparisonReport:
    """Analyze the original ninety opportunities without filtering partial cohorts."""
    if type(real_model_results) is not bool or (
        safety_passed is not None and type(safety_passed) is not bool
    ):
        raise ValueError("extension_comparison_requires_boolean_evidence_flags")
    cases = tuple(sorted(case_ids))
    if len(cases) != CASE_COUNT or len(set(cases)) != CASE_COUNT or not all(cases):
        raise ValueError("extension_comparison_requires_six_distinct_cases")
    slots = {}
    for row in rows:
        key = (row.case_id, row.repeat, row.group)
        if row.case_id not in cases or key in slots:
            raise ValueError("extension_comparison_scope_mismatch")
        slots[key] = row
    grouped = {
        group: tuple(
            slots.get((case, repeat, group), ComparisonRun(case, repeat, group, observed=False))
            for case in cases
            for repeat in range(REPEATS)
        )
        for group in GROUPS
    }
    complete = all(_usage(r) and r.task_success is not None for rs in grouped.values() for r in rs)
    blockers = []
    if not real_model_results:
        blockers.append("not_real_model_results")
    if safety_passed is not True:
        blockers.append("safety_failed" if safety_passed is False else "safety_unchecked")
    if not complete:
        blockers.append("incomplete_declared_cohort")
    rng = random.Random(SEED)
    draws = [tuple(rng.randrange(CASE_COUNT) for _ in range(CASE_COUNT)) for _ in range(RESAMPLES)]
    comparisons = {}
    for baseline, enhanced in PAIRS:
        a, b = grouped[baseline], grouped[enhanced]
        outcomes = sum(
            x.task_success is not None and y.task_success is not None
            for x, y in zip(a, b, strict=True)
        )
        usages = sum(_usage(x) and _usage(y) for x, y in zip(a, b, strict=True))
        latencies = sum(_latency(x) and _latency(y) for x, y in zip(a, b, strict=True))
        metrics = {
            "success_rate": _metric(
                a, b, draws, "success_rate", complete=outcomes == RUNS_PER_GROUP
            ),
            "tokens_per_success": _metric(
                a, b, draws, "tokens_per_success", complete=outcomes == usages == RUNS_PER_GROUP
            ),
            "latency_mean_ms": _metric(
                a, b, draws, "latency_mean_ms", complete=latencies == RUNS_PER_GROUP
            ),
        }
        success, tokens = metrics["success_rate"], metrics["tokens_per_success"]
        benefit = "not_assessable" if blockers else "no_clear_benefit"
        if not blockers:
            if success.interval_95 is not None and success.interval_95[0] > 0:
                benefit = "success_signal"
            elif (
                success.delta is not None
                and success.delta >= 0
                and tokens.interval_95 is not None
                and tokens.interval_95[1] < 0
            ):
                benefit = "efficiency_signal"
        comparisons[enhanced + "_minus_" + baseline] = PairedComparison(
            baseline, enhanced, outcomes, usages, latencies, metrics, benefit, tuple(blockers)
        )
    return ComparisonReport(
        cases,
        {group: _summary(rs, group) for group, rs in grouped.items()},
        comparisons,
        complete,
        safety_passed,
        real_model_results,
    )

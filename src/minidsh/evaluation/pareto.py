"""Trade-off analysis instead of a leaderboard.

An ablation table answers "which variant used the fewest tokens?". That is the wrong
question: a variant that spends 40% fewer tokens *and* fails 20% more tasks is not
better, it is differently wrong. The Pareto view reports which variants are
undominated on (success rate, cost axis) and, for every dominated variant, exactly
which variant dominates it and by how much.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from minidsh.domain import JsonValue

X_METRICS: tuple[str, ...] = ("tokens", "cost_usd", "steps")
_AGENT_METRIC_LABELS: dict[str, str] = {
    "tokens": "Tokens",
    "cost_usd": "Cost (USD)",
    "steps": "Steps",
}


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One ablation variant reduced to the four metrics that matter."""

    label: str
    success_rate: float
    tokens: float
    cost_usd: float
    steps: float

    @classmethod
    def from_aggregate(cls, label: str, aggregate: Mapping[str, float | int]) -> MetricPoint:
        """Build a point from a benchmark aggregate mapping."""
        return cls(
            label=label,
            success_rate=float(aggregate.get("success_rate", 0.0)),
            tokens=float(aggregate.get("average_tokens", 0.0)),
            cost_usd=float(aggregate.get("total_cost_usd", 0.0)),
            steps=float(aggregate.get("average_steps", 0.0)),
        )

    def value(self, metric: str) -> float:
        """Return one metric by name."""
        return float(getattr(self, metric))


@dataclass(frozen=True, slots=True)
class Dominance:
    """One variant proven worse than another on every axis."""

    label: str
    dominated_by: str
    success_delta: float
    metric_delta: float

    def to_json(self, metric: str) -> dict[str, JsonValue]:
        """Return a stable serialization for reports."""
        return {
            "variant": self.label,
            "dominated_by": self.dominated_by,
            "success_delta": round(self.success_delta, 4),
            f"{metric}_delta": round(self.metric_delta, 4),
        }


@dataclass(frozen=True, slots=True)
class ParetoAnalysis:
    """Frontier and dominated set for one cost axis."""

    x_metric: str
    frontier: tuple[MetricPoint, ...]
    dominated: tuple[Dominance, ...]

    def to_json(self) -> dict[str, JsonValue]:
        """Return a stable serialization for reports."""
        return {
            "x_metric": self.x_metric,
            "frontier": [point.label for point in self.frontier],
            "dominated": [item.to_json(self.x_metric) for item in self.dominated],
        }


def pareto_analysis(points: Sequence[MetricPoint], *, x_metric: str) -> ParetoAnalysis:
    """Split points into the undominated frontier and the dominated remainder."""
    if x_metric not in X_METRICS:
        msg = f"unknown cost axis: {x_metric}; choose from {list(X_METRICS)}"
        raise ValueError(msg)
    frontier: list[MetricPoint] = []
    dominated: list[Dominance] = []
    for candidate in points:
        dominator = _find_dominator(candidate, points, x_metric)
        if dominator is None:
            frontier.append(candidate)
        else:
            dominated.append(
                Dominance(
                    label=candidate.label,
                    dominated_by=dominator.label,
                    success_delta=dominator.success_rate - candidate.success_rate,
                    metric_delta=candidate.value(x_metric) - dominator.value(x_metric),
                )
            )
    ordered_frontier = tuple(
        sorted(frontier, key=lambda point: (point.value(x_metric), -point.success_rate))
    )
    return ParetoAnalysis(x_metric, ordered_frontier, tuple(dominated))


def _find_dominator(
    candidate: MetricPoint, points: Sequence[MetricPoint], x_metric: str
) -> MetricPoint | None:
    """Return the cheapest point that is at least as good on both axes."""
    best: MetricPoint | None = None
    for other in points:
        if other.label == candidate.label:
            continue
        at_least_as_good = other.success_rate >= candidate.success_rate and other.value(
            x_metric
        ) <= candidate.value(x_metric)
        strictly_better = other.success_rate > candidate.success_rate or other.value(
            x_metric
        ) < candidate.value(x_metric)
        if (
            at_least_as_good
            and strictly_better
            and (best is None or other.value(x_metric) < best.value(x_metric))
        ):
            best = other
    return best


def analyze(points: Sequence[MetricPoint]) -> tuple[ParetoAnalysis, ...]:
    """Run the analysis across every cost axis."""
    return tuple(pareto_analysis(points, x_metric=metric) for metric in X_METRICS)


def to_markdown(points: Sequence[MetricPoint], analyses: Sequence[ParetoAnalysis]) -> str:
    """Render the trade-off section of a benchmark report."""
    lines = ["## Pareto trade-off analysis", ""]
    if not points:
        lines.append("_No variants to compare._")
        return "\n".join(lines) + "\n"
    for analysis in analyses:
        label = _AGENT_METRIC_LABELS[analysis.x_metric]
        lines += [f"### Success rate vs {label}", ""]
        lines += [
            f"| Variant | Success | {label} | Undominated |",
            "| --- | --- | --- | --- |",
        ]
        frontier_labels = {point.label for point in analysis.frontier}
        lines += [
            f"| {point.label} | {point.success_rate:.0%} | {point.value(analysis.x_metric):.4g} | "
            f"{'yes' if point.label in frontier_labels else 'no'} |"
            for point in sorted(points, key=lambda item: item.value(analysis.x_metric))
        ]
        lines.append("")
        if analysis.dominated:
            lines += ["Dominated variants:", ""]
            lines += [
                f"- `{item.label}` is dominated by `{item.dominated_by}`: "
                f"{item.success_delta:+.0%} success at {item.metric_delta:+.4g} {label}."
                for item in analysis.dominated
            ]
        elif _all_indistinguishable(points, analysis.x_metric):
            lines.append(
                "All variants tie on this axis: the comparison cannot separate them, "
                "so it carries no signal."
            )
        else:
            lines.append(
                "Every variant is undominated on this axis: the comparison is a real trade-off."
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _all_indistinguishable(points: Sequence[MetricPoint], x_metric: str) -> bool:
    """Return whether every point shares both coordinates on this axis."""
    if len(points) < 2:  # noqa: PLR2004 - a single point is trivially a tie
        return True
    first = points[0]
    return all(
        point.success_rate == first.success_rate and point.value(x_metric) == first.value(x_metric)
        for point in points[1:]
    )


__all__ = [
    "X_METRICS",
    "Dominance",
    "MetricPoint",
    "ParetoAnalysis",
    "analyze",
    "pareto_analysis",
    "to_markdown",
]

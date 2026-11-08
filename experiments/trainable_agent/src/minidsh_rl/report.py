"""Offline descriptive and paired statistics. No model-based judging or new rollouts."""

import math
import random
from collections import defaultdict
from collections.abc import Sequence


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarize(rows: list[dict[str, object]], *, declared: int) -> dict[str, object]:
    """Keep declared opportunities distinct from checked outcomes and actual spend."""
    verified = [row for row in rows if isinstance(row.get("success"), bool)]
    successes = sum(row.get("success") is True for row in verified)
    tokens = sum(int(str(row["tokens"])) for row in rows if isinstance(row.get("tokens"), int))
    calls = [
        float(str(row["tool_calls"])) for row in rows if isinstance(row.get("tool_calls"), int)
    ]
    durations = [
        float(str(row["duration_ms"]))
        for row in rows
        if isinstance(row.get("duration_ms"), (int, float))
    ]
    return {
        "declared": declared,
        "verified": len(verified),
        "successes": successes,
        "success_rate_declared": successes / declared if declared else None,
        "success_rate_verified": successes / len(verified) if verified else None,
        "coverage": len(verified) / declared if declared else None,
        "total_tokens": tokens,
        "tokens_per_success": tokens / successes if successes else None,
        "mean_tool_calls": sum(calls) / len(calls) if calls else None,
        "tool_sample_count": len(calls),
        "latency_p50_ms": _percentile(durations, 0.5),
        "latency_p95_ms": _percentile(durations, 0.95),
        "latency_sample_count": len(durations),
    }


def paired_interval(pairs: list[tuple[str, bool, bool]]) -> dict[str, object]:
    """Family cluster percentile bootstrap; repeats are never independent tasks."""
    groups: dict[str, list[float]] = defaultdict(list)
    for family, base, rl in pairs:
        groups[family].append(float(rl) - float(base))
    if not pairs:
        return {"difference": None, "ci95": None, "families": 0, "pairs": 0}
    names = sorted(groups)
    rng = random.Random(20261106)
    draws = []
    for _ in range(10000):
        selected = rng.choices(names, k=len(names))
        draws.append(
            sum(sum(groups[name]) for name in selected)
            / sum(len(groups[name]) for name in selected)
        )
    return {
        "difference": sum(sum(values) for values in groups.values()) / len(pairs),
        "ci95": [_percentile(draws, 0.025), _percentile(draws, 0.975)],
        "families": len(groups),
        "pairs": len(pairs),
        "bootstrap_repetitions": 10000,
        "seed": 20261106,
        "quantile_method": "nearest_rank",
        "limitation": "single training seed and four test families; exploratory interval only",
    }

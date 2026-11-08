"""Efficiency must not hide failed consumption or treat zero success as free work."""

import pytest
from minidsh_rl.secondary_statistics import analyze, measures


def row(policy: str, tokens: int, *, success: bool) -> dict[str, object]:
    return {
        "policy": policy,
        "tokens": tokens,
        "success": success,
        "tool_calls": 4,
        "duration_ms": 1000,
        "family": "family",
        "case_id": "case",
        "repeat": 0,
        "seed": 17,
    }


def test_failure_consumption_is_kept_in_cost_per_success() -> None:
    rows = [
        row("base", 100, success=True),
        row("rl", 80, success=True),
        row("base", 100, success=False),
        row("rl", 120, success=True),
    ]
    assert measures(rows)["token_per_success_relative_change"] == -0.5


def test_zero_success_ratio_is_unknown_not_zero_cost() -> None:
    rows = [row("base", 100, success=True), row("rl", 80, success=False)]
    assert measures(rows)["token_per_success_relative_change"] is None
    result = analyze({"runs": rows})
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["token_per_success_relative_change"]["undefined_draws"] == 10000


def test_mismatched_pair_is_rejected() -> None:
    rows = [row("base", 100, success=True), row("rl", 80, success=True)]
    rows[1]["case_id"] = "different"
    with pytest.raises(ValueError, match="mismatched pair"):
        analyze({"runs": rows})

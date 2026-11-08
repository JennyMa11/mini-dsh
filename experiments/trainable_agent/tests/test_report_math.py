"""Paired family statistics retain the full denominator and missing evidence."""

from minidsh_rl.report import paired_interval, summarize


def test_identical_policies_have_zero_paired_delta() -> None:
    pairs = [(str(family), bool(i % 2), bool(i % 2)) for family in range(4) for i in range(8)]
    result = paired_interval(pairs)
    assert result["difference"] == 0
    assert result["ci95"] == [0, 0]
    assert result["families"] == 4


def test_failures_remain_in_tokens_per_success_and_zero_is_unmeasured() -> None:
    rows: list[dict[str, object]] = [
        {"success": True, "tokens": 100, "tool_calls": 2, "duration_ms": 10},
        {"success": False, "tokens": 300, "tool_calls": 6, "duration_ms": 30},
    ]
    result = summarize(rows, declared=3)
    assert result["successes"] == 1
    assert result["declared"] == 3
    assert result["verified"] == 2
    assert result["tokens_per_success"] == 400
    assert result["mean_tool_calls"] == 4
    assert result["coverage"] == 2 / 3
    assert summarize([], declared=0)["success_rate_declared"] is None
    assert summarize([rows[1]], declared=1)["tokens_per_success"] is None

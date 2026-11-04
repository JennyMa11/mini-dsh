"""Contracts for the separately frozen, larger-budget follow-up cohort."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from minidsh.benchmark.followup import (
    FollowupProtocol,
    followup_schedule,
    latency_reconciled,
    paired_summary,
    row_options,
    summarize_rows,
)
from minidsh.benchmark.harness_study import StudyProtocol
from minidsh.domain import LatencyBreakdown

PROTOCOL = Path("benchmarks/harness_study/v2/protocol.json")


def test_latency_audit_accepts_trace_rounding_but_rejects_missing_time() -> None:
    rounded = LatencyBreakdown(model_ms=0.097, tool_ms=215.023, harness_ms=17.186, total_ms=232.307)
    assert latency_reconciled(rounded)
    missing = LatencyBreakdown(model_ms=0.097, tool_ms=215.023, harness_ms=17.186, total_ms=237.307)
    assert not latency_reconciled(missing)


def test_frozen_followup_retains_all_tasks_and_balanced_pairs() -> None:
    protocol = FollowupProtocol.from_file(PROTOCOL)
    schedule = followup_schedule(protocol)
    assert len(schedule) == 108
    baseline = [r for r in schedule if r.group == "regression"]
    assert len(baseline) == 36
    assert set(Counter(r.task_id for r in baseline).values()) == {3}
    mechanisms = [r for r in schedule if r.group != "regression"]
    assert len(mechanisms) == 72
    for group in ("context", "output", "recovery"):
        rows = [r for r in mechanisms if r.group == group]
        assert len({r.task_id for r in rows}) == 4
        assert sum(r.variant == "A" for r in rows) == 12
        assert sum(r.variant == "B" for r in rows) == 12
        pairs = list(dict.fromkeys(r.pair_id for r in rows))
        orders = [tuple(r.variant for r in rows if r.pair_id == p) for p in pairs]
        assert orders.count(("A", "B")) == orders.count(("B", "A")) == 6
    assert protocol.total_token_budget >= 36 * 32000 + 72 * 64000


def test_followup_does_not_mutate_v1_or_allow_result_based_tuning() -> None:
    old = StudyProtocol.from_file(Path("benchmarks/harness_study/v1/protocol.json"))
    new = FollowupProtocol.from_file(PROTOCOL)
    assert old.max_tokens_per_run == 8000
    assert new.mechanism_tokens == 64000
    changed = new.model_dump(mode="json")
    changed["mechanism_tokens"] = 128000
    with pytest.raises(ValidationError):
        FollowupProtocol.model_validate(changed)


@pytest.mark.parametrize(
    ("group", "field"),
    [
        ("context", "context_strategy"),
        ("output", "tool_output_limit"),
        ("recovery", "error_recovery_enabled"),
    ],
)
def test_followup_changes_exactly_one_mechanism(group: str, field: str) -> None:
    schedule = followup_schedule(FollowupProtocol.from_file(PROTOCOL))
    pair = next(r.pair_id for r in schedule if r.group == group)
    a, b = sorted((r for r in schedule if r.pair_id == pair), key=lambda r: r.variant)
    left, right = asdict(row_options(a)), asdict(row_options(b))
    assert [key for key in left if left[key] != right[key]] == [field]


def test_summary_keeps_not_run_unknown_and_failed_consumption() -> None:
    rows: list[dict[str, Any]] = [
        {
            "status": "finished",
            "success": True,
            "full_success": True,
            "constraint_compliant": True,
            "tokens": 200,
            "unknown_usage_calls": 0,
        },
        {
            "status": "failed",
            "success": False,
            "full_success": False,
            "constraint_compliant": False,
            "tokens": 500,
            "unknown_usage_calls": 1,
        },
        {"status": "not_run", "success": False, "tokens": 0},
    ]
    report = summarize_rows(rows)
    assert report["declared_runs"] == 3
    assert report["executed_runs"] == 2
    assert report["successes"] == 1
    assert report["total_tokens"] == 700
    assert report["unknown_usage_calls"] == 1
    assert report["token_usage_complete"] is False
    assert report["known_tokens_per_success"] == 700
    assert report["tokens_per_success"] is None


def test_paired_uncertainty_clusters_repeats_and_separates_trigger_from_outcome() -> None:
    rows: list[dict[str, Any]] = []
    for task in ("one", "two", "three", "four"):
        for repeat in range(3):
            for variant, tokens in (("A", 100), ("B", 80)):
                rows.append(
                    {
                        "group": "context",
                        "task_id": task,
                        "pair_id": f"{task}_{repeat}",
                        "variant": variant,
                        "status": "budget_exhausted",
                        "tokens": tokens,
                        "input_tokens": tokens - 10,
                        "mechanism_triggered": True,
                        "success": False,
                        "full_success": False,
                        "constraint_compliant": False,
                        "unknown_usage_calls": 0,
                    }
                )
    report = paired_summary(rows, "context")
    assert report["complete_pairs"] == 12
    assert report["triggered_pairs"] == 12
    assert report["task_clusters"] == 4
    assert report["both_constraint_compliant_pairs"] == 0
    assert report["mean_token_delta_B_minus_A"] == -20
    assert report["token_delta_cluster_bootstrap_95"] == [-20, -20]
    assert paired_summary(rows, "context") == report


def test_missing_pair_is_retained_without_fabricated_token_delta() -> None:
    row: dict[str, Any] = {
        "group": "recovery",
        "task_id": "one",
        "pair_id": "p",
        "variant": "A",
        "status": "finished",
        "tokens": 100,
        "input_tokens": 80,
        "mechanism_triggered": True,
        "success": True,
        "full_success": True,
        "constraint_compliant": True,
        "unknown_usage_calls": 0,
    }
    other = dict(row, variant="B", status="not_run", tokens=0)
    report = paired_summary([row, other], "recovery")
    assert report["declared_pairs"] == 1
    assert report["complete_pairs"] == 0
    assert report["mean_token_delta_B_minus_A"] is None

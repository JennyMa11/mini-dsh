"""Contracts for evidence-aware, read-only harness scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from minidsh.cli import main
from minidsh.domain import JsonValue
from minidsh.evaluation.harness_metrics import (
    ScoringEvidence,
    aggregate_harness_metrics,
    score_harness,
)
from minidsh.evaluation.tracefile import TraceRecord, TraceRun


def _event(name: str, step: int = 1, **data: JsonValue) -> TraceRecord:
    return TraceRecord(name, "r1", step, data)


def _trace(*events: TraceRecord) -> TraceRun:
    return TraceRun.from_records(events)


def _evidence(**values: object) -> ScoringEvidence:
    return ScoringEvidence.model_validate({"run_id": "r1", "source": "fixture-verifier", **values})


def test_missing_evidence_and_zero_opportunities_are_unmeasured() -> None:
    score = score_harness(_trace(_event("run_finished", status="finished", steps=1)))
    for name in ("task_success_rate", "recovery_rate", "unsafe_action_block_rate"):
        assert score.rates[name].value is None
    assert score.rates["tool_error_rate"].denominator == 0
    assert score.to_json()["schema_version"] == "1.0"
    assert "cost_usd" not in score.measurements


def test_tool_execution_errors_are_separate_from_schema_and_policy_rejections() -> None:
    score = score_harness(
        _trace(
            _event("tool_call_completed", status="invalid"),
            _event("tool_call_completed", status="denied"),
            _event("tool_call_completed", status="succeeded", result={"exit_code": 1}),
            _event("tool_call_completed", status="timed_out"),
            _event("tool_call_completed", status="succeeded", result={"exit_code": 0}),
        )
    )
    assert score.rates["tool_error_rate"].value == pytest.approx(2 / 3)
    assert score.rates["invalid_tool_call_rate"].value == pytest.approx(1 / 5)
    assert score.rates["permission_denied_rate"].value == pytest.approx(1 / 5)
    assert score.rates["tool_call_accuracy"].value is None
    assert score.rates["unsafe_action_block_rate"].value is None


def test_repeated_failure_proxy_ignores_successful_repetition_and_changed_arguments() -> None:
    records: list[TraceRecord] = []
    for index, (argument, exit_code) in enumerate(((1, 1), (1, 1), (2, 1), (2, 0), (2, 0)), 1):
        call_id = str(index)
        records.extend(
            (
                _event(
                    "tool_call_started", tool="shell", call_id=call_id, arguments={"x": argument}
                ),
                _event(
                    "tool_call_completed",
                    tool="shell",
                    call_id=call_id,
                    status="succeeded",
                    result={"exit_code": exit_code},
                ),
            )
        )
    score = score_harness(_trace(*records))
    assert score.rates["repeated_failed_action_rate"].value == pytest.approx(1 / 5)
    assert score.rates["recovery_rate"].value is None


def test_labeled_checks_keep_unknowns_and_do_not_count_correct_refusal_as_failure() -> None:
    evidence = _evidence(
        task_success=True,
        recovery_checks={"episode-1": True, "episode-2": None},
        unsafe_action_checks={"escape": True, "network": False},
        safe_action_checks={"read": False},
        secret_checks={"trace": True, "output": None},
        policy_checks={"no-write": False},
    )
    score = score_harness(_trace(_event("run_finished", status="finished")), evidence=evidence)
    recovery = score.rates["recovery_rate"]
    assert (recovery.numerator, recovery.denominator, recovery.eligible) == (1, 1, 2)
    assert recovery.coverage == 0.5
    assert score.rates["task_success_rate"].value == 1
    assert score.rates["unsafe_action_block_rate"].value == 0.5
    assert score.rates["safe_action_false_block_rate"].value == 1
    assert score.rates["secret_leak_rate"].value == 0
    assert score.rates["secret_leak_rate"].coverage == 0.5
    assert score.rates["policy_violation_rate"].value == 1


def test_budget_overrun_is_reported_even_when_the_candidate_passes() -> None:
    trace = _trace(
        _event("model_call_started"),
        _event("model_call_completed", input_tokens=100, output_tokens=20, cached_input_tokens=50),
        _event("run_finished", status="budget_exhausted", steps=2, tool_calls=1, duration_ms=30),
    )
    score = score_harness(
        trace,
        evidence=_evidence(
            task_success=True,
            budget={
                "max_tokens": 100,
                "max_steps": 3,
                "max_tool_calls": 2,
                "max_duration_seconds": 1,
            },
        ),
    )
    assert score.rates["task_success_rate"].value == 1
    assert score.rates["constraint_satisfaction"].value == 0.75
    assert score.rates["constraint_satisfied_run_rate"].value == 0
    assert score.measurements["token_overrun"] == 20
    assert score.measurements["total_tokens"] == 120
    assert score.rates["cached_input_share"].value == 0.5


def test_unknown_usage_and_unfinished_requests_are_not_zero_cost_or_budget_passes() -> None:
    trace = _trace(
        _event("model_call_started"),
        _event("model_call_failed", usage_known=False, input_tokens=None, output_tokens=None),
        _event("model_call_started", 2),
        _event("run_finished", 2, status="cancelled", steps=2, duration_ms=30),
    )
    score = score_harness(trace, evidence=_evidence(budget={"max_tokens": 100}))
    assert score.measurements["unknown_usage_calls"] == 2
    assert score.measurements["token_usage_complete"] is False
    assert score.rates["constraint_satisfaction"].value is None


def test_explicit_unknown_usage_keeps_numeric_lower_bounds_and_never_passes_the_budget() -> None:
    trace = _trace(
        _event("model_call_started"),
        _event(
            "model_call_completed",
            usage_known=False,
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=50,
        ),
        _event("run_finished", status="budget_exhausted", steps=1, unknown_usage_calls=1),
    )
    score = score_harness(trace, evidence=_evidence(budget={"max_tokens": 100}))
    assert score.measurements["total_tokens"] == 120
    assert score.measurements["cached_input_tokens"] == 50
    assert score.measurements["unknown_usage_calls"] == 1
    assert score.measurements["token_usage_complete"] is False
    assert score.measurements["token_overrun"] == 20
    assert score.rates["constraint_satisfied_run_rate"].value == 0
    assert score.rates["cache_hit_rate"].denominator == 0
    assert score.rates["cached_input_share"].value is None


def test_cache_share_is_token_weighted_and_cache_hit_rate_is_call_weighted() -> None:
    trace = _trace(
        _event("model_call_completed", input_tokens=100, output_tokens=1, cached_input_tokens=50),
        _event("model_call_completed", input_tokens=10, output_tokens=1, cached_input_tokens=0),
        _event("model_call_completed", input_tokens=10, output_tokens=1),
    )
    score = score_harness(trace)
    assert score.rates["cached_input_share"].value == pytest.approx(50 / 110)
    assert score.rates["cached_input_share"].coverage == pytest.approx(110 / 120)
    assert score.rates["cache_hit_rate"].value == 0.5
    assert score.rates["cache_hit_rate"].coverage == pytest.approx(2 / 3)


def test_partial_goals_do_not_invent_full_task_success() -> None:
    score = score_harness(
        _trace(_event("run_finished")),
        evidence=_evidence(
            goal_checks={"parser": True, "escaping": False},
        ),
    )
    assert score.rates["goal_completion_rate"].value == 0.5
    assert score.rates["partial_success_rate"].value == 1
    assert score.rates["task_success_rate"].value is None


def test_evidence_requires_matching_run_strict_booleans_and_unique_constraint_names() -> None:
    with pytest.raises(ValidationError):
        _evidence(recovery_checks={"first": "yes"})
    with pytest.raises(ValueError, match="run_id"):
        score_harness(_trace(_event("run_finished")), evidence=_evidence(run_id="other"))
    with pytest.raises(ValueError, match="constraint"):
        score_harness(
            _trace(_event("run_finished")),
            evidence=_evidence(
                budget={"max_tokens": 10},
                constraint_checks={"budget.tokens": True},
            ),
        )


def test_aggregation_pools_opportunities_and_reports_small_sample_latency() -> None:
    scores = [
        score_harness(
            _trace(_event("run_finished", steps=1, duration_ms=10)),
            evidence=_evidence(recovery_checks={"a": True}),
        ),
        score_harness(
            _trace(_event("run_finished", steps=3, duration_ms=30)),
            evidence=_evidence(recovery_checks={"b": False, "c": False}),
        ),
    ]
    report = aggregate_harness_metrics(scores)
    rates = report["rates"]
    assert isinstance(rates, dict)
    recovery = rates["recovery_rate"]
    assert isinstance(recovery, dict)
    assert recovery["value"] == pytest.approx(1 / 3)
    measurements = report["measurements"]
    assert isinstance(measurements, dict)
    assert measurements["latency_p50_ms"] == 20
    assert measurements["latency_p95_ms"] == 29
    assert measurements["latency_sample_count"] == 2
    assert measurements["avg_steps"] == 2


def test_metrics_cli_scores_saved_trace_without_provider_or_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "name": "run_finished",
                "run_id": "r1",
                "step_id": 1,
                "data": {"status": "finished", "steps": 1},
            }
        )
        + "\n"
    )
    with pytest.raises(SystemExit) as result:
        main(["trace", "metrics", str(trace)])
    assert result.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rates"]["task_success_rate"]["value"] is None
    assert payload["run_id"] == "r1"

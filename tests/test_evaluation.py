"""Unit tests for the trace-derived evaluation views."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minidsh.domain import FailureCategory, JsonValue
from minidsh.evaluation import (
    MetricPoint,
    TraceError,
    TraceRun,
    build_replay,
    classify_failure,
    diff_traces,
    latency_from_trace,
    pareto,
    tool_latencies,
)
from minidsh.evaluation.diff import to_markdown as diff_to_markdown
from minidsh.evaluation.replay import to_markdown as replay_to_markdown
from minidsh.evaluation.taxonomy import summarize, summarize_layers
from minidsh.evaluation.tracefile import TraceRecord


def _record(name: str, step_id: int = 0, **data: JsonValue) -> TraceRecord:
    return TraceRecord(name=name, run_id="run-1", step_id=step_id, data=dict(data))


def _run(*records: TraceRecord) -> TraceRun:
    return TraceRun.from_records(records)


def _finished(**data: JsonValue) -> TraceRecord:
    return _record("run_finished", 4, status="finished", **data)


def test_failed_model_usage_survives_replay_and_latency_fallback() -> None:
    trace = _run(
        _record(
            "model_call_completed",
            1,
            input_tokens=10,
            output_tokens=2,
            cached_input_tokens=4,
            cost_usd=0.01,
            duration_ms=20.0,
        ),
        _record(
            "model_call_failed",
            2,
            usage_known=True,
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=60,
            cost_usd=0.02,
            duration_ms=30.0,
        ),
        _record(
            "model_call_failed",
            3,
            usage_known=False,
            input_tokens=None,
            output_tokens=None,
            cached_input_tokens=None,
            cost_usd=None,
            duration_ms=10.0,
        ),
        _record("run_finished", 3, status="failed", duration_ms=65.0),
    )
    replay = build_replay(trace)
    assert trace.total_tokens() == replay.tokens == (110, 22, 64)
    assert trace.cost_usd() == replay.cost_usd == pytest.approx(0.03)
    assert replay.steps[1].state == replay.steps[2].state == "failed"
    assert replay.latency.model_ms == 60.0
    assert replay.latency.model_calls == 3
    failed = _run(
        _record(
            "run_finished",
            1,
            status="failed",
            failure_reason="ModelCallError[bad_response]: invalid action",
        )
    )
    assert classify_failure(status="failed", verified=None, trace=failed).category is (
        FailureCategory.MODEL_REASONING_FAILURE
    )


# --- tracefile -----------------------------------------------------------------


def test_trace_file_round_trips_records(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {
                    "name": "run_started",
                    "run_id": "r1",
                    "step_id": 0,
                    "data": {"task_length": 4},
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "name": "model_call_completed",
                    "run_id": "r1",
                    "step_id": 1,
                    "data": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cached_input_tokens": 6,
                        "cost_usd": 0.001,
                        "duration_ms": 12.0,
                    },
                },
                {
                    "name": "run_finished",
                    "run_id": "r1",
                    "step_id": 1,
                    "data": {"status": "finished", "duration_ms": 30.0},
                },
            )
        ),
        encoding="utf-8",
    )

    trace = TraceRun.from_file(path)

    assert trace.status() == "finished"
    assert trace.total_tokens() == (10, 4, 6)
    assert trace.cost_usd() == pytest.approx(0.001)
    assert [step.step_id for step in trace.steps()] == [1]
    assert trace.steps()[0].model_call is not None


def test_malformed_trace_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(TraceError):
        TraceRun.from_file(path)


def test_missing_trace_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TraceError):
        TraceRun.from_file(tmp_path / "absent.jsonl")


def test_trace_groups_records_into_ordered_steps() -> None:
    trace = _run(
        _record("context_built", 1, strategy="full_history", estimated_tokens=20),
        _record("model_call_completed", 1, input_tokens=20, output_tokens=5),
        _record("tool_call_completed", 1, tool="read_file", status="succeeded"),
        _record("context_built", 2, strategy="full_history", estimated_tokens=40),
        _record("model_call_completed", 2, input_tokens=40, output_tokens=6),
    )

    steps = trace.steps()

    assert [step.step_id for step in steps] == [1, 2]
    assert steps[0].tool_names == ("read_file",)
    assert steps[1].tool_names == ()


# --- taxonomy ------------------------------------------------------------------


def test_verified_success_is_not_a_failure() -> None:
    diagnosis = classify_failure(status="finished", verified=True, trace=_run(_finished()))

    assert diagnosis.category is FailureCategory.NONE
    assert diagnosis.layer == "success"
    assert diagnosis.is_harness_layer is False


def test_cancelled_run_is_not_a_failure() -> None:
    diagnosis = classify_failure(status="cancelled", verified=None, trace=_run(_finished()))

    assert diagnosis.category is FailureCategory.NONE


def test_budget_exhaustion_is_attributed_to_the_harness_layer() -> None:
    trace = _run(
        _record(
            "run_finished",
            9,
            status="budget_exhausted",
            failure_reason="step budget exhausted",
        )
    )

    diagnosis = classify_failure(status="budget_exhausted", verified=None, trace=trace)

    assert diagnosis.category is FailureCategory.BUDGET_EXHAUSTED
    assert diagnosis.layer == "harness"


def test_unrecovered_permission_denial_is_reported() -> None:
    trace = _run(
        _record("permission_decided", 1, tool="shell", allowed=False, reason="missing permissions"),
        _record("tool_call_completed", 1, tool="shell", status="denied"),
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is FailureCategory.PERMISSION_DENIED
    assert diagnosis.layer == "harness"


def test_a_denial_the_agent_worked_around_is_not_the_cause() -> None:
    trace = _run(
        _record("permission_decided", 1, tool="shell", allowed=False, reason="missing permissions"),
        _record("tool_call_completed", 1, tool="shell", status="denied"),
        _record("tool_call_completed", 2, tool="shell", status="succeeded"),
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is not FailureCategory.PERMISSION_DENIED


def test_invalid_tool_call_is_reported_before_verification() -> None:
    trace = _run(
        _record(
            "tool_call_completed",
            1,
            tool="read_file",
            status="invalid",
            error={"code": "invalid_arguments"},
        ),
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is FailureCategory.INVALID_TOOL_CALL
    assert "read_file" in diagnosis.evidence[0]


def test_unrecovered_tool_error_is_reported() -> None:
    trace = _run(
        _record("tool_call_completed", 1, tool="test", status="failed", error={"code": "nonzero"}),
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is FailureCategory.TOOL_ERROR


def test_repeated_identical_tool_results_are_flagged_as_a_stuck_loop() -> None:
    trace = _run(
        *[
            _record("tool_call_completed", step, tool="search", status="succeeded", result="none")
            for step in (1, 2, 3)
        ],
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is FailureCategory.LOOP_STUCK
    assert diagnosis.layer == "agent"


def test_verification_failure_without_inspection_is_a_context_miss() -> None:
    trace = _run(
        _record("context_built", 1, strategy="summary", estimated_tokens=10, omitted_messages=7),
        _record("model_call_completed", 1, input_tokens=10, output_tokens=2),
        _record("tool_call_completed", 1, tool="patch_file", status="succeeded"),
        _finished(),
    )

    diagnosis = classify_failure(status="finished", verified=False, trace=trace)

    assert diagnosis.category is FailureCategory.CONTEXT_MISS
    assert diagnosis.layer == "agent"


def test_verification_failure_after_inspection_is_a_plain_wrong_change() -> None:
    trace = _run(
        _record("context_built", 1, strategy="summary", estimated_tokens=10, omitted_messages=7),
        _record("tool_call_completed", 1, tool="read_file", status="succeeded"),
        _record("tool_call_completed", 2, tool="patch_file", status="succeeded"),
        _finished(),
    )

    diagnosis = classify_failure(
        status="finished", verified=False, trace=trace, verifier_exit_code=1
    )

    assert diagnosis.category is FailureCategory.VERIFICATION_FAILED
    assert "verifier_exit_code:1" in diagnosis.evidence


def test_provider_failure_is_attributed_to_the_environment() -> None:
    """A reason carrying only the class name keeps its historical diagnosis.

    Traces written before the loop recorded error codes, and hand-built fixtures,
    look like this.  Re-reading an archived trace must not change what it says.
    """
    trace = _run(
        _record(
            "run_finished",
            1,
            status="failed",
            failure_reason="ModelAdapterError: provider returned a transient error (HTTP 500)",
        )
    )

    diagnosis = classify_failure(status="failed", verified=None, trace=trace)

    assert diagnosis.category is FailureCategory.PROVIDER_ERROR
    assert diagnosis.layer == "environment"


def test_a_malformed_model_response_is_attributed_to_the_agent() -> None:
    """A response the runtime cannot use is a bad generation, not a bad network.

    `ModelAdapterError` covers both a provider outage and a model that emitted
    malformed tool arguments, so the exception class alone cannot classify it.  The
    code can, and getting this wrong is not cosmetic: it files a model failure under
    `environment`, the layer read as "not the model's fault", so a run that failed
    because the model wrote bad JSON looks like a provider hiccup.  A real hard-suite
    run failed exactly this way and was briefly read as one.
    """
    trace = _run(
        _record(
            "run_finished",
            1,
            status="failed",
            failure_reason=(
                "ModelAdapterError[invalid_tool_arguments]: "
                "model returned invalid tool arguments for shell"
            ),
        )
    )

    diagnosis = classify_failure(status="failed", verified=None, trace=trace)

    assert diagnosis.category is FailureCategory.MODEL_REASONING_FAILURE
    assert diagnosis.layer == "agent"
    assert "invalid_tool_arguments" in diagnosis.evidence[0]


def test_an_unparseable_model_response_is_attributed_to_the_agent() -> None:
    trace = _run(
        _record(
            "run_finished",
            1,
            status="failed",
            failure_reason=(
                "ModelAdapterError[bad_response]: model returned an unparseable response"
            ),
        )
    )

    diagnosis = classify_failure(status="failed", verified=None, trace=trace)

    assert diagnosis.category is FailureCategory.MODEL_REASONING_FAILURE
    assert diagnosis.layer == "agent"


@pytest.mark.parametrize(
    "code",
    [
        "auth_error",
        "client_error",
        "missing_credentials",
        "network_error",
        "rate_limited",
        "timeout",
    ],
)
def test_exchange_failure_codes_stay_in_the_environment(code: str) -> None:
    """Everything that is not the model's output is still the environment's problem.

    The point of splitting by code is not to move failures out of `environment` but
    to stop moving *model* failures into it, so the other direction needs its own
    guard: a coded provider outage must not become an agent failure.
    """
    trace = _run(
        _record(
            "run_finished",
            1,
            status="failed",
            failure_reason=f"ModelAdapterError[{code}]: the exchange failed",
        )
    )

    diagnosis = classify_failure(status="failed", verified=None, trace=trace)

    assert diagnosis.category is FailureCategory.PROVIDER_ERROR
    assert diagnosis.layer == "environment"


def test_layer_summary_separates_harness_from_agent_failures() -> None:
    diagnoses = [
        classify_failure(status="finished", verified=True, trace=_run(_finished())),
        classify_failure(
            status="finished",
            verified=False,
            trace=_run(
                _record("permission_decided", 1, tool="shell", allowed=False, reason="denied"),
                _finished(),
            ),
        ),
        classify_failure(status="finished", verified=False, trace=_run(_finished())),
    ]

    assert summarize(diagnoses) == {
        "none": 1,
        "permission_denied": 1,
        "verification_failed": 1,
    }
    assert summarize_layers(diagnoses) == {
        "success": 1,
        "harness": 1,
        "agent": 1,
        "environment": 0,
    }


# --- latency -------------------------------------------------------------------


def test_latency_splits_model_tool_and_harness_time() -> None:
    trace = _run(
        _record("model_call_completed", 1, duration_ms=100.0),
        _record("model_call_completed", 2, duration_ms=200.0),
        _record("tool_call_completed", 1, tool="test", duration_ms=50.0),
        _record("run_finished", 2, status="finished", duration_ms=1000.0),
    )

    breakdown = latency_from_trace(trace)

    assert breakdown.model_ms == pytest.approx(300.0)
    assert breakdown.tool_ms == pytest.approx(50.0)
    assert breakdown.harness_ms == pytest.approx(650.0)
    assert breakdown.model_calls == 2
    assert breakdown.model_share == pytest.approx(0.3)
    assert breakdown.mean_model_ms == pytest.approx(150.0)


def test_latency_prefers_the_breakdown_the_loop_recorded() -> None:
    """A failed model call leaves no ``model_call_completed`` to sum.

    Reconstructing from per-call events would lose the provider's time to the
    harness residual, which inverts what the split is for.  The recorded block
    is authoritative; reconstruction is only the fallback.
    """
    trace = _run(
        _record("model_call_started", 1),
        _record(
            "run_finished",
            1,
            status="failed",
            duration_ms=3500.0,
            latency={
                "model_ms": 3499.0,
                "tool_ms": 0.0,
                "harness_ms": 1.0,
                "total_ms": 3500.0,
                "model_calls": 1,
                "tool_calls": 0,
            },
        ),
    )

    breakdown = latency_from_trace(trace)

    assert breakdown.model_ms == pytest.approx(3499.0)
    assert breakdown.harness_ms == pytest.approx(1.0)
    assert breakdown.model_calls == 1


def test_latency_falls_back_to_reconstruction_for_older_traces() -> None:
    """Traces written before the recorded block still produce a split."""
    trace = _run(
        _record("model_call_completed", 1, duration_ms=100.0),
        _record("run_finished", 1, status="finished", duration_ms=400.0),
    )

    breakdown = latency_from_trace(trace)

    assert breakdown.model_ms == pytest.approx(100.0)
    assert breakdown.harness_ms == pytest.approx(300.0)


def test_tool_latencies_are_aggregated_and_sorted() -> None:
    trace = _run(
        _record("tool_call_completed", 1, tool="read_file", duration_ms=5.0),
        _record("tool_call_completed", 2, tool="test", duration_ms=40.0),
        _record("tool_call_completed", 3, tool="read_file", duration_ms=5.0),
    )

    aggregates = tool_latencies(trace)

    assert [item.tool for item in aggregates] == ["test", "read_file"]
    assert aggregates[1].calls == 2
    assert aggregates[1].total_ms == pytest.approx(10.0)
    assert aggregates[1].mean_ms == pytest.approx(5.0)


# --- pareto --------------------------------------------------------------------


def _point(label: str, success: float, tokens: float, cost: float, steps: float) -> MetricPoint:
    return MetricPoint(label, success, tokens, cost, steps)


def test_pareto_frontier_keeps_only_undominated_variants() -> None:
    points = [
        _point("cheap_and_accurate", 1.0, 100.0, 0.01, 3.0),
        _point("expensive_and_accurate", 1.0, 300.0, 0.03, 3.0),
        _point("cheap_and_wrong", 0.5, 90.0, 0.009, 3.0),
    ]

    analysis = pareto.pareto_analysis(points, x_metric="tokens")

    assert [item.label for item in analysis.frontier] == ["cheap_and_wrong", "cheap_and_accurate"]
    assert [item.label for item in analysis.dominated] == ["expensive_and_accurate"]
    assert analysis.dominated[0].dominated_by == "cheap_and_accurate"
    assert analysis.dominated[0].metric_delta == pytest.approx(200.0)


def test_ties_are_not_dominance() -> None:
    points = [
        _point("a", 1.0, 100.0, 0.01, 3.0),
        _point("b", 1.0, 100.0, 0.01, 3.0),
    ]

    analysis = pareto.pareto_analysis(points, x_metric="tokens")

    assert len(analysis.frontier) == 2
    assert analysis.dominated == ()


def test_markdown_reports_a_tie_instead_of_a_fake_trade_off() -> None:
    points = [_point("a", 1.0, 100.0, 0.01, 4.0), _point("b", 1.0, 100.0, 0.01, 4.0)]

    markdown = pareto.to_markdown(points, pareto.analyze(points))

    assert "cannot separate them" in markdown
    assert "real trade-off" not in markdown


def test_unknown_cost_axis_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown cost axis"):
        pareto.pareto_analysis([_point("a", 1.0, 1.0, 0.0, 1.0)], x_metric="latency")


# --- replay --------------------------------------------------------------------


def test_replay_reconstructs_every_step_without_a_model() -> None:
    trace = _run(
        _record(
            "context_built",
            1,
            strategy="full_history",
            estimated_tokens=20,
            omitted_messages=0,
        ),
        _record(
            "model_call_completed",
            1,
            finish_reason="tool_calls",
            content="reading",
            duration_ms=9.0,
            input_tokens=20,
            output_tokens=3,
            cached_input_tokens=0,
        ),
        _record(
            "tool_call_completed",
            1,
            tool="read_file",
            status="succeeded",
            duration_ms=4.0,
            attempt=1,
            truncated=False,
            result={"path": "a.py"},
        ),
        _record("model_call_completed", 2, finish_reason="stop", content="done"),
        _record("run_finished", 2, status="finished", duration_ms=40.0),
    )

    replayed = build_replay(trace)

    assert replayed.status == "finished"
    assert [step.state for step in replayed.steps] == ["continue", "finish"]
    assert replayed.steps[0].tool_calls[0].tool == "read_file"
    assert replayed.tokens == (20, 3, 0)
    assert replayed.latency.harness_ms == pytest.approx(27.0)


def test_replay_markdown_uses_the_loop_vocabulary() -> None:
    trace = _run(
        _record("context_built", 1, strategy="full", estimated_tokens=1),
        _record("model_call_completed", 1, finish_reason="stop", content="done"),
        _record("run_finished", 1, status="finished", duration_ms=5.0),
    )

    markdown = build_replay(trace)

    rendered = replay_to_markdown(markdown)
    assert "**Context**" in rendered
    assert "**Model**" in rendered
    assert "**State**" in rendered


# --- diff ----------------------------------------------------------------------


def _trace_with_tool(tool: str, tokens: int) -> TraceRun:
    return _run(
        _record("context_built", 1, strategy="full", estimated_tokens=tokens),
        _record("model_call_completed", 1, finish_reason="tool_calls", input_tokens=tokens),
        _record("tool_call_completed", 1, tool=tool, status="succeeded"),
        _record("run_finished", 1, status="finished", duration_ms=10.0),
    )


def test_diff_locates_the_first_divergent_step() -> None:
    left = _trace_with_tool("read_file", 100)
    right = _trace_with_tool("search", 400)

    result = diff_traces(left, right, left_label="base", right_label="retrieval")

    assert result.divergence_step == 1
    assert result.divergence_kind is not None
    assert "tool calls diverged" in result.divergence_kind
    assert result.token_delta == 300
    assert result.token_delta_pct == pytest.approx(3.0)
    assert result.steps[0].context_delta == 300


def test_diff_of_identical_traces_reports_no_divergence() -> None:
    trace = _trace_with_tool("read_file", 100)

    result = diff_traces(trace, trace)

    assert result.divergence_step is None
    assert result.token_delta == 0


def test_diff_markdown_is_readable() -> None:
    rendered = diff_to_markdown(
        diff_traces(_trace_with_tool("read_file", 1), _trace_with_tool("search", 2))
    )

    assert "First behavioural divergence at **step 1**" in rendered
    assert "| Step |" in rendered

"""Contract checks for the frozen local study and interruption recovery."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from minidsh.benchmark.formal import (
    FORMAL_VARIANTS,
    Checkpoint,
    StudyKey,
    frozen_tree_digest,
    paired_summary,
    schedule,
)
from minidsh.benchmark.runner import (
    BenchmarkError,
    BenchmarkManifest,
    BenchmarkRunner,
    BenchmarkTask,
    TaskMetrics,
)
from minidsh.domain import ModelResponse
from minidsh.evaluation.inference import inference_from_trace
from minidsh.evaluation.tracefile import TraceRecord, TraceRun


def _result(task_id: str, tokens: int, *, success: bool = True) -> TaskMetrics:
    return TaskMetrics(
        task_id=task_id,
        success=success,
        status="finished",
        steps=2,
        tokens=tokens,
        cost_usd=0,
        tool_calls=1,
        latency_ms=100,
        failure_type=None,
        verifier_exit_code=0,
        trace_path="trace.jsonl",
    )


def test_frozen_tree_digest_detects_content_and_path_changes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("answer = 1\n")
    original = frozen_tree_digest(tmp_path)
    assert len(original) == 71
    assert original.startswith("sha256:")
    (tmp_path / "a.py").write_text("answer = 2\n")
    assert frozen_tree_digest(tmp_path) != original
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    assert frozen_tree_digest(tmp_path) != original


def test_runner_rejects_changed_frozen_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "case"
    verifier = tmp_path / "verification" / "case"
    solution = tmp_path / "solutions" / "case"
    for tree in (fixture, verifier, solution):
        tree.mkdir(parents=True)
        (tree / "file.py").write_text("x = 1\n")
    task = BenchmarkTask(
        "case",
        "fix",
        fixture,
        ("python", "verify.py"),
        verification_overlay=verifier,
        repo_commit=frozen_tree_digest(fixture),
        verification_commit=frozen_tree_digest(verifier),
        solution_commit=frozen_tree_digest(solution),
    )
    BenchmarkRunner._validate_manifest(BenchmarkManifest("frozen", "1", (task,)))
    (verifier / "file.py").write_text("x = 2\n")
    with pytest.raises(BenchmarkError, match="verifier hash mismatch"):
        BenchmarkRunner._validate_manifest(BenchmarkManifest("frozen", "1", (task,)))


def test_formal_variants_have_five_cumulative_stages() -> None:
    assert [variant.name for variant in FORMAL_VARIANTS] == [
        "raw_200k",
        "bounded_20k",
        "progressive_repo",
        "budget_context",
        "loop_guard_recovery",
    ]
    assert FORMAL_VARIANTS[0].tool_output_limit == 200_000
    assert FORMAL_VARIANTS[1].tool_output_limit == 20_000
    assert FORMAL_VARIANTS[1].context_strategy == "full"
    assert FORMAL_VARIANTS[2].progressive_tools_enabled
    assert FORMAL_VARIANTS[3].context_strategy == "budget"
    assert FORMAL_VARIANTS[4].local_first_enabled


def test_model_format_retry_is_explicit_in_response_contract() -> None:
    assert ModelResponse().format_retries == 0


def test_inference_speed_uses_token_weighted_rate() -> None:
    trace = TraceRun.from_records(
        (
            TraceRecord(
                "model_call_completed",
                "run",
                1,
                {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "inference": {
                        "prefill_tokens_per_second": 100.0,
                        "decode_tokens_per_second": 10.0,
                        "peak_memory_gb": 2.0,
                    },
                },
            ),
            TraceRecord(
                "model_call_completed",
                "run",
                2,
                {
                    "input_tokens": 900,
                    "output_tokens": 90,
                    "inference": {
                        "prefill_tokens_per_second": 300.0,
                        "decode_tokens_per_second": 30.0,
                        "peak_memory_gb": 3.0,
                    },
                },
            ),
        )
    )
    summary = inference_from_trace(trace)
    assert summary["prefill_tokens_per_second"] == 250.0
    assert summary["decode_tokens_per_second"] == 25.0
    assert summary["peak_memory_gb"] == 3.0


def test_schedule_pairs_variants_and_rotates_model_order() -> None:
    runs = schedule(("one", "two"), repeats=3, models=("4b", "8b"), variants=("a", "b"))
    assert len(runs) == 24
    assert [(r.variant, r.task_id) for r in runs[:2]] == [("a", "one"), ("b", "one")]
    assert [(r.model, r.variant) for r in runs[:4]] == [
        ("4b", "a"),
        ("4b", "b"),
        ("4b", "b"),
        ("4b", "a"),
    ]
    assert runs[8].model == "8b"
    assert runs[8].variant == "b"


def test_checkpoint_rejects_mismatch_duplicates_and_missing_trace(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "records.jsonl", "protocol-1")
    key = StudyKey("matrix", "4b", "baseline", "task", 0)
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    result = _result("task", 100)
    checkpoint.append(key, result)
    assert checkpoint.get(key) == result
    with pytest.raises(BenchmarkError, match="duplicate"):
        checkpoint.append(key, result)
    with pytest.raises(BenchmarkError, match="protocol"):
        Checkpoint(tmp_path / "records.jsonl", "protocol-2")
    raw = json.loads((tmp_path / "records.jsonl").read_text().splitlines()[0])
    assert raw["metrics"] == asdict(result)
    trace.unlink()
    with pytest.raises(BenchmarkError, match="trace"):
        Checkpoint(tmp_path / "records.jsonl", "protocol-1")


def test_paired_summary_clusters_repeats_by_task() -> None:
    pairs = {
        "one": [(_result("one", 100), _result("one", 50))] * 3,
        "two": [(_result("two", 200, success=False), _result("two", 100))] * 3,
    }
    summary = paired_summary(pairs, seed=7, bootstrap_samples=100)
    assert summary["tasks"] == 2
    assert summary["paired_runs"] == 6
    assert summary["token_delta_mean"] == -75
    assert summary["success_delta_mean"] == 0.5
    assert len(summary["token_delta_ci95"]) == 2

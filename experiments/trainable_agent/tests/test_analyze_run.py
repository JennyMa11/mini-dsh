"""Derived results must refuse conflicting independent evidence, not hide it."""

import hashlib
import json
from pathlib import Path

import pytest
from minidsh_rl.analyze_run import audit_run, repeated_cat_reads, trace_metrics

from minidsh.domain import JsonValue
from minidsh.evaluation.tracefile import TraceRecord, TraceRun


def fixture(directory: Path) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": "run",
        "case_id": "task",
        "seed": 17,
        "split": "test",
        "success": True,
        "status": "finished",
        "tool_calls": 0,
        "duration_ms": 10.0,
        "policy": "base",
        "tokens": 3,
    }
    candidate = b"def solve(): return 1\n"
    (directory / "candidate.py").write_bytes(candidate)
    result = dict(
        row, policy_id="base", total_tokens=3, candidate_hash=hashlib.sha256(candidate).hexdigest()
    )
    (directory / "result.json").write_text(json.dumps(result))
    (directory / "verification").mkdir()
    (directory / "verification/result.json").write_text(
        json.dumps(
            {
                "case_id": "task",
                "passed": True,
                "criterion_version": "lightweight_v2",
            }
        )
    )
    (directory / "tokens").mkdir()
    (directory / "tokens/turn_000.json").write_text(
        json.dumps(
            {
                "run_id": "run",
                "policy_id": "base",
                "prompt_token_ids": [1, 2],
                "completion_token_ids": [3],
                "old_logprobs": [-0.1],
            }
        )
    )
    records = [
        ("run_started", {}),
        (
            "model_call_completed",
            {
                "input_tokens": 2,
                "output_tokens": 1,
                "usage_known": True,
            },
        ),
        (
            "run_finished",
            {
                "status": "finished",
                "steps": 1,
                "tokens": 3,
                "tool_calls": 0,
                "duration_ms": 8.0,
                "unknown_usage_calls": 0,
            },
        ),
    ]
    (directory / "trace.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "name": name,
                    "run_id": "run",
                    "step_id": 1,
                    "data": data,
                }
            )
            for name, data in records
        )
    )
    return row


def test_audit_binds_verdict_candidate_and_exact_tokens(tmp_path: Path) -> None:
    row = fixture(tmp_path)
    detail, score = audit_run(tmp_path, row)
    assert detail["model_cost_upper_cny"] == "0.02"
    assert score.rates["task_success_rate"].numerator == 1
    (tmp_path / "candidate.py").write_text("tampered")
    with pytest.raises(ValueError, match="candidate hash mismatch"):
        audit_run(tmp_path, row)


def test_audit_does_not_replace_independent_verdict_with_queue_label(tmp_path: Path) -> None:
    row = fixture(tmp_path)
    path = tmp_path / "verification/result.json"
    verdict = json.loads(path.read_text())
    verdict["passed"] = False
    path.write_text(json.dumps(verdict))
    with pytest.raises(ValueError, match="independent verdict mismatch"):
        audit_run(tmp_path, row)


def test_audit_detects_dropped_token_sidecar(tmp_path: Path) -> None:
    row = fixture(tmp_path)
    (tmp_path / "tokens/turn_000.json").unlink()
    with pytest.raises(ValueError, match="token reconciliation failed"):
        audit_run(tmp_path, row)


def test_missing_cache_telemetry_stays_unknown(tmp_path: Path) -> None:
    _, score = audit_run(tmp_path, fixture(tmp_path))
    result = trace_metrics([score])
    rates = result["rates"]
    assert isinstance(rates, dict)
    assert rates["cache_hit_rate"]["value"] is None
    assert rates["cached_input_share"]["coverage"] == 0


def test_repeat_proxy_excludes_truncation_and_failed_calls() -> None:
    result: dict[str, JsonValue] = {
        "argv": ["cat", "implementation.py"],
        "stdout": "same",
        "stderr": "",
        "exit_code": 0,
    }
    records = tuple(
        TraceRecord("tool_call_completed", "run", i, {"result": result, "truncated": truncated})
        for i, truncated in enumerate((False, False, True))
    )
    assert repeated_cat_reads(TraceRun.from_records(records)) == {
        "numerator": 1,
        "denominator": 2,
        "eligible": 2,
    }

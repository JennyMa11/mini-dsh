"""Training admission must use the full declared screening cohort, not lucky samples."""

from pathlib import Path

import pytest
from minidsh_rl.screen_report import admission, analyze


def test_admission_requires_complete_evidence_and_moderate_base_success() -> None:
    assert admission(declared=128, completed=127, successes=64, unknown_usage=0) is False
    assert admission(declared=128, completed=128, successes=64, unknown_usage=0) is True
    assert admission(declared=128, completed=128, successes=4, unknown_usage=0) is False
    assert admission(declared=128, completed=128, successes=126, unknown_usage=0) is False
    assert admission(declared=128, completed=128, successes=64, unknown_usage=1) is False


def test_missing_summary_cannot_admit_training(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        analyze(tmp_path)


def test_full_audit_refuses_a_disagreeing_independent_verdict(tmp_path: Path) -> None:
    import hashlib  # noqa: PLC0415 - local synthetic artifact builder
    import json  # noqa: PLC0415 - local synthetic artifact builder

    from minidsh_rl.screen_v2 import screening_queue  # noqa: PLC0415 - frozen queue fixture

    rows = screening_queue()
    source = tmp_path / "snapshot.py"
    source.write_text("# frozen fixture")
    (tmp_path / "protocol.json").write_text(
        json.dumps({"sources": {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}})
    )
    (tmp_path / "summary.json").write_text(json.dumps({"declared": 128, "completed": 128}))
    candidate = "def solve(*args): return 0\n"
    for index, row in enumerate(rows):
        row.update(
            {
                "run_id": f"run_{index}",
                "success": bool(index % 2),
                "tokens": 3,
                "tool_calls": 0,
                "duration_ms": 1.0,
                "status": "finished",
                "failure_type": None,
            }
        )
        run = tmp_path / str(row["run_id"])
        (run / "tokens").mkdir(parents=True)
        (run / "verification").mkdir()
        (run / "candidate.py").write_text(candidate)
        (run / "result.json").write_text(
            json.dumps(
                dict(
                    row,
                    total_tokens=3,
                    candidate_hash=hashlib.sha256(candidate.encode()).hexdigest(),
                )
            )
        )
        (run / "verification/result.json").write_text(
            json.dumps(
                {
                    "case_id": row["case_id"],
                    "passed": row["success"],
                    "criterion_version": "expansion_v2",
                }
            )
        )
        (run / "tokens/turn_000.json").write_text(
            json.dumps({"prompt_token_ids": [1, 2], "completion_token_ids": [3]})
        )
        events = [
            ("run_started", {}),
            ("model_call_completed", {"input_tokens": 2, "output_tokens": 1}),
            ("run_finished", {"status": "finished", "unknown_usage_calls": 0}),
        ]
        (run / "trace.jsonl").write_text(
            "\n".join(
                json.dumps({"name": name, "run_id": row["run_id"], "step_id": 1, "data": data})
                for name, data in events
            )
        )
    (tmp_path / "queue.json").write_text(json.dumps(rows))
    report = analyze(tmp_path)
    assert report["ready_for_smoke"] is True
    assert report["successes"] == 64
    verdict = tmp_path / "run_1/verification/result.json"
    changed = json.loads(verdict.read_text())
    changed["passed"] = False
    verdict.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="verifier mismatch"):
        analyze(tmp_path)

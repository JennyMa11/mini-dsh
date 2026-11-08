"""Offline diagnosis must fail closed when Trace and raw samples disagree."""

import json
from pathlib import Path

import pytest
from minidsh_rl.diagnose_formats import diagnose


def write_run(root: Path, *, rejection: bool) -> None:
    run = root / "run"
    (run / "tokens").mkdir(parents=True)
    (run / "tokens/turn_000.json").write_text(
        json.dumps(
            {
                "raw_text": "<tool_call>unfinished",
                "run_id": "run",
                "call_index": 0,
                "completion_token_ids": [1, 2],
                "stop_reason": "length",
            }
        )
    )
    events = ["run_started", "run_finished"]
    if rejection:
        events.insert(1, "model_output_rejected")
    (run / "trace.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "name": name,
                    "run_id": "run",
                    "step_id": 1,
                    "data": {},
                }
            )
            for name in events
        )
    )


def test_rejection_links_to_length_stop_without_repair(tmp_path: Path) -> None:
    write_run(tmp_path, rejection=True)
    result = diagnose((tmp_path,))
    assert result["sample_calls"] == 1
    assert result["length_stopped_rejections"] == 1
    assert result["parse_error_counts"] == {"one complete tool envelope required": 1}


def test_missing_event_cannot_be_silently_scored(tmp_path: Path) -> None:
    write_run(tmp_path, rejection=False)
    with pytest.raises(ValueError, match="mismatch"):
        diagnose((tmp_path,))

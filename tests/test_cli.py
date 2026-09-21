"""Smoke tests for the command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minidsh.cli import main


def test_doctor_reports_healthy_environment(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["doctor"])

    assert exit_info.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["python_ok"] is True


def test_version_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("mini-dsh ")


def _write_trace(path: Path, *, tool: str, context_tokens: int) -> None:
    records = [
        {
            "name": "context_built",
            "run_id": "r1",
            "step_id": 1,
            "data": {"strategy": "full_history", "estimated_tokens": context_tokens},
        },
        {
            "name": "model_call_completed",
            "run_id": "r1",
            "step_id": 1,
            "data": {
                "finish_reason": "tool_calls",
                "content": "working",
                "input_tokens": context_tokens,
                "output_tokens": 4,
                "duration_ms": 12.0,
            },
        },
        {
            "name": "tool_call_completed",
            "run_id": "r1",
            "step_id": 1,
            "data": {
                "tool": tool,
                "status": "succeeded",
                "duration_ms": 3.0,
                "attempt": 1,
                "result": {"ok": True},
            },
        },
        {
            "name": "run_finished",
            "run_id": "r1",
            "step_id": 1,
            "data": {"status": "finished", "duration_ms": 40.0},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_trace_replay_renders_every_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = tmp_path / "run.jsonl"
    _write_trace(trace, tool="read_file", context_tokens=100)

    with pytest.raises(SystemExit) as exit_info:
        main(["trace", "replay", str(trace)])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "**Context**" in out
    assert "`read_file`" in out
    assert "**State**" in out


def test_trace_replay_supports_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = tmp_path / "run.jsonl"
    _write_trace(trace, tool="search", context_tokens=50)

    with pytest.raises(SystemExit):
        main(["trace", "replay", str(trace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "finished"
    assert payload["steps"] == 1


def test_trace_diff_reports_the_token_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_trace(left, tool="read_file", context_tokens=100)
    _write_trace(right, tool="read_file", context_tokens=300)

    with pytest.raises(SystemExit) as exit_info:
        main(["trace", "diff", str(left), str(right), "--left-label", "base"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "`base` vs `right`" in out
    assert "+200" in out


def test_report_renders_a_saved_benchmark_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "name": "suite",
                "aggregate": {"tasks": 1, "success_rate": 1.0},
                "failure_layers": {"success": 1, "harness": 0, "agent": 0, "environment": 0},
                "failure_categories": {"none": 1},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["report", str(report)])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "# Benchmark report: suite" in out
    assert "## Outcome by layer" in out


def test_unknown_model_profile_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "do something",
                "--workspace",
                str(tmp_path),
                "--model-profile",
                "does-not-exist",
            ]
        )

    assert exit_info.value.code == 2
    assert "unknown model profile" in capsys.readouterr().err


def test_a_paid_profile_without_a_rate_card_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "do something",
                "--workspace",
                str(tmp_path),
                "--model-profile",
                "deepseek",
            ]
        )

    assert exit_info.value.code == 2
    assert "no declared rate card" in capsys.readouterr().err

"""Only explicitly allowed numeric metrics and bounded public config can leave."""

import json
from pathlib import Path

import pytest
from minidsh_rl.tracking import MetricRecorder, RunSpec


def test_reject_arbitrary_text_secrets_and_unrecognized_metrics(tmp_path: Path) -> None:
    recorder = MetricRecorder(tmp_path, RunSpec("unit", "component", "gspo_multiturn"))
    with pytest.raises(ValueError, match="allowlisted"):
        recorder.log(0, {"api_key": 1.0})
    with pytest.raises(ValueError, match="allowlisted"):
        recorder.log(0, {"reward/mean": float("nan")})
    assert not (tmp_path / "metrics.jsonl").exists()


def test_local_record_is_fact_source_and_steps_monotonic(tmp_path: Path) -> None:
    recorder = MetricRecorder(tmp_path, RunSpec("unit", "component", "gspo_multiturn"))
    recorder.log(0, {"reward/mean": 0.5, "rollout/completed": 4.0})
    with pytest.raises(ValueError, match="steps must increase"):
        recorder.log(0, {"reward/mean": 0.0})
    item = json.loads((tmp_path / "metrics.jsonl").read_text())
    assert item["metrics"]["reward/mean"] == 0.5
    assert item["schema_version"] == 1
    assert "+00:00" in item["recorded_at"]


def test_closed_directory_cannot_be_silently_overwritten(tmp_path: Path) -> None:
    MetricRecorder(tmp_path, RunSpec("one", "component", "gspo_multiturn"))
    with pytest.raises(FileExistsError):
        MetricRecorder(tmp_path, RunSpec("two", "component", "gspo_multiturn"))


def test_remote_failure_keeps_local_record_without_error_payload(tmp_path: Path) -> None:
    class Broken:
        def log(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret simulated token")

    recorder = MetricRecorder(tmp_path, RunSpec("unit", "component", "gspo_multiturn"))
    recorder.remote = Broken()
    recorder.log(0, {"reward/mean": 0.5})
    assert "secret" not in (tmp_path / "tracking-error.json").read_text()
    assert (tmp_path / "metrics.jsonl").exists()

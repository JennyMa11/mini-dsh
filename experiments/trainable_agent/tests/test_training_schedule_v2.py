"""Pilot is the fixed prefix of the extended schedule; no test tasks enter training."""

from pathlib import Path

import pytest
from minidsh_rl.train_v2 import training_schedule


def test_full_schedule_and_pilot_prefix_are_fixed() -> None:
    full = training_schedule("extend")
    pilot = training_schedule("pilot")
    assert len(full) == 120
    assert len(pilot) == 40
    assert pilot == full[:40]
    assert all(task.split == "train" for task in full)
    assert len({task.case_id for task in full}) == 48
    assert training_schedule("extend") == full


def test_engineering_smoke_remains_separate() -> None:
    smoke = training_schedule("smoke")
    assert len(smoke) == 2
    assert all(task.split == "smoke" for task in smoke)


def test_missing_screening_cannot_create_a_training_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minidsh_rl import train_v2  # noqa: PLC0415 - isolate runtime gate dependencies

    monkeypatch.setattr(train_v2, "ROOT", tmp_path)
    monkeypatch.setattr(train_v2, "_require_core_gates", lambda: None)
    monkeypatch.setattr(train_v2, "_require_gates", lambda _image: None)
    with pytest.raises(FileNotFoundError):
        train_v2._prepare(tmp_path / "never-created", "fixture", "smoke", None, None, 120)
    assert not (tmp_path / "never-created").exists()

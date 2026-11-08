"""No automatic resubmission after a crash with an unknown update outcome."""

from pathlib import Path

import pytest
from minidsh_rl.transaction import UpdateJournal


def test_submitted_update_survives_restart_and_cannot_be_replayed(tmp_path: Path) -> None:
    path = tmp_path / "update.json"
    journal = UpdateJournal.create(path, "datum_hash")
    journal.advance("backward_submitted")
    journal.advance("backward_confirmed")
    journal.advance("optim_submitted")
    restored = UpdateJournal.load(path)
    assert restored.state == "optim_submitted"
    assert not restored.safe_to_resume
    with pytest.raises(ValueError, match="transition"):
        restored.advance("optim_submitted")
    with pytest.raises(FileExistsError):
        UpdateJournal.create(path, "same_hash")


def test_confirmed_update_requires_checkpoint_before_safe_resume(tmp_path: Path) -> None:
    journal = UpdateJournal.create(tmp_path / "update.json", "hash")
    for state in ("backward_submitted", "backward_confirmed", "optim_submitted", "optim_confirmed"):
        journal.advance(state)
    assert not journal.safe_to_resume
    journal.advance("checkpointed", checkpoint="trio://train/state")
    assert UpdateJournal.load(journal.path).safe_to_resume


def test_zero_signal_is_terminal_without_optimizer_submission(tmp_path: Path) -> None:
    journal = UpdateJournal.create(tmp_path / "update.json", "hash")
    journal.advance("skipped")
    assert journal.safe_to_resume
    with pytest.raises(ValueError, match="transition"):
        journal.advance("backward_submitted")

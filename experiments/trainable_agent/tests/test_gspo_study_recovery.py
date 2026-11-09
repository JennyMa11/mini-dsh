"""Rollback is restricted to terminal backward-only failure and exact trained Adam state."""

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.gspo_study_recovery import (
    READINESS_ATTEMPTS,
    READINESS_DELAY,
    eligible,
    equal_state,
    optimizer,
    ready_checkpoint,
)
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_rollout import PolicyBinding


def checkpoint() -> Checkpoint:
    return Checkpoint(
        execution_kind="provider",
        sampler_uri="trio://sampler",
        optimizer_uri="trio://state",
        policy=PolicyBinding(
            kind="provider",
            policy_id="policy_a",
            snapshot_sha256="a" * 64,
            tokenizer_sha256="b" * 64,
        ),
    )


def test_only_terminal_backward_may_rollback() -> None:
    cp = checkpoint()
    state = CurveState(
        plan_sha256="a" * 64, kind="provider", pending="update", current=cp, initial=cp
    )
    metadata = {
        "owned": True,
        "status": "ENDED",
        "corrupted": False,
        "base_model": "Qwen/Qwen3.5-4B",
        "seed": 17,
        "lora_rank": 16,
    }
    eligible(state, {"state": "backward_submitted"}, metadata)
    for phase in ("optim_submitted", "optim_confirmed", "checkpointed"):
        with pytest.raises(ValueError, match="backward-only"):
            eligible(state, {"state": phase}, metadata)
    with pytest.raises(ValueError, match="terminal owned"):
        eligible(state, {"state": "backward_submitted"}, dict(metadata, status="RUNNING"))
    with pytest.raises(ValueError, match="terminal owned"):
        eligible(state, {"state": "backward_submitted"}, dict(metadata, owned=False))


def archive(path: Path, *, step: int, empty: bool = False) -> None:
    state = (
        {}
        if empty
        else {
            0: {
                "step": torch.tensor(step),
                "exp_avg": torch.tensor([0.1, 0.2]),
                "exp_avg_sq": torch.tensor([0.01, 0.02]),
            }
        }
    )
    content = io.BytesIO()
    torch.save({"state": state, "param_groups": [{"params": [0], "lr": 1e-5}]}, content)
    data = content.getvalue()
    with tarfile.open(path, "w") as output:
        item = tarfile.TarInfo("optimizer.pt")
        item.size = len(data)
        output.addfile(item, io.BytesIO(data))


def test_nonempty_optimizer_tensor_equality_and_steps(tmp_path: Path) -> None:
    first = tmp_path / "first.tar"
    same = tmp_path / "same.tar"
    different = tmp_path / "different.tar"
    archive(first, step=19)
    archive(same, step=19)
    archive(different, step=0)
    assert equal_state(optimizer(first), optimizer(same))
    assert not equal_state(optimizer(first), optimizer(different))
    empty = tmp_path / "empty.tar"
    archive(empty, step=0, empty=True)
    with pytest.raises(ValueError, match="nonempty trained optimizer"):
        optimizer(empty)
    assert not equal_state(torch.tensor([1.0]), torch.tensor([1.0], dtype=torch.float64))


async def test_checkpoint_upload_readiness_is_polled_with_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        calls = 0

        async def list_training_run_checkpoints(self, uid: str) -> SimpleNamespace:
            assert uid == "owned"
            self.calls += 1
            status = "UPLOADING" if self.calls == 1 else "READY"
            return SimpleNamespace(
                checkpoints=[SimpleNamespace(checkpoint_uri="trio://cp", status=status)]
            )

    async def no_sleep(delay: int) -> None:
        assert delay == READINESS_DELAY

    monkeypatch.setattr("minidsh_rl.gspo_study_recovery.asyncio.sleep", no_sleep)
    client = Client()
    result = await ready_checkpoint(client, "owned", "trio://cp")
    assert result.status == "READY"
    assert client.calls == 2
    with pytest.raises(ValueError, match="readiness deadline"):
        await ready_checkpoint(client, "owned", "trio://missing")
    assert client.calls == 2 + READINESS_ATTEMPTS

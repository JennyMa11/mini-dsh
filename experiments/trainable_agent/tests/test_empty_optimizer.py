"""A fresh initializer cannot stand in for a checkpoint with learned momentum."""

import io
import tarfile
from pathlib import Path

import pytest
from minidsh_rl.empty_optimizer import compare_empty_optimizers, empty_optimizer


def optimizer_archive(path: Path, *, nonempty: bool = False, learning_rate: float = 0.0001) -> None:
    torch = pytest.importorskip("torch")
    stream = io.BytesIO()
    torch.save(
        {
            "state": {0: {"step": 1}} if nonempty else {},
            "param_groups": [{"params": [0, 1], "lr": learning_rate}],
        },
        stream,
    )
    raw = stream.getvalue()
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo("optimizer.pt")
        item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))


def test_nonempty_optimizer_is_never_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "nonempty.tar"
    optimizer_archive(path, nonempty=True)
    with pytest.raises(ValueError, match="nonempty"):
        empty_optimizer(path)


def test_fresh_optimizer_requires_equal_hyperparameters(tmp_path: Path) -> None:
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    third = tmp_path / "third.tar"
    optimizer_archive(first)
    optimizer_archive(second)
    optimizer_archive(third, learning_rate=0.1)
    compare_empty_optimizers(first, second)
    with pytest.raises(ValueError, match="groups changed"):
        compare_empty_optimizers(first, third)

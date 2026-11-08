"""LoRA weight delta checks use actual safetensors bytes, without GPU or Torch."""

import io
import math
import tarfile
from pathlib import Path

import numpy as np
from minidsh_rl.weight_probe import tensor_blobs, weight_delta
from safetensors.numpy import save


def archive(path: Path, value: float) -> None:
    blob = save({"lora_B": np.array([0.0, value], dtype=np.float32)})
    with tarfile.open(path, "w") as output:
        info = tarfile.TarInfo("adapter_model.safetensors")
        info.size = len(blob)
        output.addfile(info, io.BytesIO(blob))


def test_zero_update_and_known_scaled_gradients(tmp_path: Path) -> None:
    left, right = tmp_path / "initial.tar", tmp_path / "updated.tar"
    archive(left, 0.0)
    archive(right, 3.0)
    assert weight_delta(left, left) == 0
    assert weight_delta(left, right) == 3.0
    archive(right, 6.0)
    assert weight_delta(left, right) == 6.0
    assert math.isfinite(weight_delta(left, right))


def test_archive_members_are_read_without_extracting_paths(tmp_path: Path) -> None:
    path = tmp_path / "weights.tar"
    blob = save({"B": np.array([1.0], dtype=np.float32)})
    with tarfile.open(path, "w") as output:
        info = tarfile.TarInfo("../outside.safetensors")
        info.size = len(blob)
        output.addfile(info, io.BytesIO(blob))
    assert tensor_blobs(path) == {"../outside.safetensors": blob}
    assert not (tmp_path.parent / "outside.safetensors").exists()


def test_bfloat16_adapter_values_convert_without_torch() -> None:
    import struct  # noqa: PLC0415 - tiny binary fixture

    from minidsh_rl.weight_probe import tensor_arrays  # noqa: PLC0415

    header = b'{"B":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]}}'
    header += b" " * (-len(header) % 8)
    blob = struct.pack("<Q", len(header)) + header + b"\x80\x3f\x00\x40"
    assert tensor_arrays(blob)["B"].tolist() == [1.0, 2.0]

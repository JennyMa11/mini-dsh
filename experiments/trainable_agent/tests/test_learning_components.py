"""Small real safetensors recheck evidence; all values are contract fixtures."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest
from minidsh_rl.learning_components import KERNELS, review_components, verify_components
from minidsh_rl.learning_gates import PROJECT
from safetensors.numpy import save


def fixture(root: Path) -> None:
    source = {}
    protocol = root / "core-protocol-v2"
    protocol.mkdir()
    with tarfile.open(protocol / "source.tar.gz", "w:gz") as archive:
        for name in (*KERNELS, "uv.lock"):
            relative = (
                "experiments/trainable_agent/uv.lock"
                if name == "uv.lock"
                else "experiments/trainable_agent/src/minidsh_rl/" + name
            )
            raw = (
                b'[[package]]\nname="pytrio"\nversion="0.2.9"\n'
                if name == "uv.lock"
                else (PROJECT / relative).read_bytes()
            )
            source[relative] = hashlib.sha256(raw).hexdigest()
            member = tarfile.TarInfo(relative)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    (protocol / "source.json").write_text(json.dumps(source))
    values = {
        "training-probe-03/baseline": 0,
        "training-probe-04/zero": 0,
        "training-probe-04/single": 1,
        "training-probe-04/duplicate": 1,
        "training-probe-04/split": 0.5,
        "resume-probe-01/before": 0,
        "resume-probe-01/original": 10,
        "resume-probe-01/replay": 10.2,
    }
    for name, value in values.items():
        path = root / (name + ".archive")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(save({"lora_B": np.array([0, value], dtype=np.float32)}))
    (root / "training-probe-04/result.json").write_text(
        json.dumps(
            {
                "mask_passed": True,
                "reduction": "sample_mean",
                "duplicate_ratio": 1,
                "split_ratio": 0.5,
            }
        )
    )
    (root / "resume-probe-01/result.json").write_text(
        json.dumps(
            {
                "passed": True,
                "threshold": 0.03,
                "relative_difference": float(np.float32(10.2) - np.float32(10)) / 10,
            }
        )
    )


def test_reuse_recomputes_weights_and_rejects_changed_result(tmp_path: Path) -> None:
    fixture(tmp_path)
    output = tmp_path / "review.json"
    evidence = review_components(tmp_path, output)
    assert evidence.metrics["zero_delta"] == 0
    assert evidence.origin == "historical_component_reuse"
    assert verify_components(output) == evidence
    raw = json.loads(output.read_text())
    raw["metrics"]["duplicate_ratio"] = 999
    output.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="proof changed"):
        verify_components(output)


def test_passed_flag_does_not_override_nonzero_mask_delta(tmp_path: Path) -> None:
    fixture(tmp_path)
    (tmp_path / "training-probe-04/zero.archive").write_bytes(
        save({"lora_B": np.array([0, 1], dtype=np.float32)})
    )
    with pytest.raises(ValueError, match="zero-mask"):
        review_components(tmp_path, tmp_path / "review.json")

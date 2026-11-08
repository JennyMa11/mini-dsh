"""Initial LoRA validation uses tiny local tensor archives, no model service."""

import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest
from minidsh_rl.learning_initial import validate_initial_adapter
from safetensors.numpy import save


def archive(path: Path, *, b: float = 0, width: int = 2) -> None:
    files = {
        "adapter_config.json": json.dumps(
            {
                "r": 16,
                "lora_alpha": 32,
                "lora_dropout": 0,
                "bias": "none",
                "target_modules": ["q_proj"],
            }
        ).encode(),
        "adapter_model.safetensors": save(
            {
                "layer.lora_A.weight": np.ones((2, width), dtype=np.float32),
                "layer.lora_B.weight": np.full((width, 2), b, dtype=np.float32),
            }
        ),
    }
    with tarfile.open(path, "w") as output:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            output.addfile(member, io.BytesIO(raw))


def test_initial_adapter_requires_zero_b_and_matching_structure(tmp_path: Path) -> None:
    reference, candidate = tmp_path / "reference.tar", tmp_path / "candidate.tar"
    archive(reference)
    archive(candidate)
    result = validate_initial_adapter(reference, candidate)
    assert result["b_matrix_count"] == 1
    assert result["b_norm"] == 0
    archive(candidate, b=1)
    with pytest.raises(ValueError, match="zero"):
        validate_initial_adapter(reference, candidate)
    archive(candidate, width=3)
    with pytest.raises(ValueError, match="structure"):
        validate_initial_adapter(reference, candidate)

"""Recheck historical component evidence from source and weights; no SDK sessions."""

import hashlib
import json
import math
import tarfile
import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from minidsh_rl.learning_gates import PROJECT
from minidsh_rl.learning_rollout import Digest
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest
from minidsh_rl.weight_probe import weight_delta

KERNELS = ("contracts.py", "datum.py", "trainer.py", "pytrio_backend.py")
_FILES = {
    "source": "core-protocol-v2/source.json",
    "archive": "core-protocol-v2/source.tar.gz",
    "mask": "training-probe-04/result.json",
    "resume": "resume-probe-01/result.json",
    "baseline": "training-probe-03/baseline.archive",
    **{
        name: f"training-probe-04/{name}.archive"
        for name in ("zero", "single", "duplicate", "split")
    },
    **{
        f"resume_{name}": f"resume-probe-01/{name}.archive"
        for name in ("before", "original", "replay")
    },
}
TOLERANCE = 0.03


class FilePin(FrozenRecord):
    path: str
    sha256: Digest

    def verify(self) -> Path:
        path = Path(self.path)
        if path.is_symlink() or not path.is_file() or digest(path) != self.sha256:
            raise ValueError("pinned evidence changed")
        return path


class ComponentEvidence(FrozenRecord):
    schema_version: Literal[1] = 1
    origin: Literal["historical_component_reuse"] = "historical_component_reuse"
    sdk_version: Literal["0.2.9"] = "0.2.9"
    kernels: dict[str, Digest]
    inputs: dict[str, FilePin]
    metrics: dict[str, float]
    new_provider_calls: Literal[0] = 0
    new_training_calls: Literal[0] = 0


def _inspect(  # noqa: PLR0912 - ordered source, archive and numerical evidence checks
    inputs: dict[str, FilePin],
) -> tuple[dict[str, str], dict[str, float]]:
    if set(inputs) != set(_FILES) or version("pytrio") != "0.2.9":
        raise ValueError("component evidence/SDK identity mismatch")
    paths = {key: pin.verify() for key, pin in inputs.items()}
    source = json.loads(paths["source"].read_text())
    kernels = {}
    lock_name = "experiments/trainable_agent/uv.lock"
    with tarfile.open(paths["archive"]) as archive:
        for name in (*KERNELS, "uv.lock"):
            relative = (
                lock_name
                if name == "uv.lock"
                else "experiments/trainable_agent/src/minidsh_rl/" + name
            )
            member = archive.getmember(relative)
            if not member.isfile():
                raise ValueError("component archive member is not a file")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("component archive member missing")
            raw = stream.read()
            if hashlib.sha256(raw).hexdigest() != source[relative]:
                raise ValueError("historical source archive changed")
            if name == "uv.lock":
                packages = tomllib.loads(raw.decode())["package"]
                if [p["version"] for p in packages if p["name"] == "pytrio"] != ["0.2.9"]:
                    raise ValueError("historical SDK version changed")
            else:
                kernels[relative] = digest(PROJECT / relative)
                if kernels[relative] != source[relative]:
                    raise ValueError("numerical kernel changed; fresh probe required")
    deltas = {
        name: weight_delta(paths["baseline"], paths[name])
        for name in ("zero", "single", "duplicate", "split")
    }
    update = weight_delta(paths["resume_before"], paths["resume_original"])
    difference = weight_delta(paths["resume_original"], paths["resume_replay"])
    if deltas["zero"] != 0 or deltas["single"] <= 0 or update <= 0:
        raise ValueError("historical zero-mask/signal gate failed")
    metrics = {
        "zero_delta": deltas["zero"],
        "signal_delta": deltas["single"],
        "duplicate_ratio": deltas["duplicate"] / deltas["single"],
        "split_ratio": deltas["split"] / deltas["single"],
        "resume_relative_difference": difference / update,
    }
    if (
        not all(math.isfinite(v) for v in metrics.values())
        or abs(metrics["duplicate_ratio"] - 1) > TOLERANCE
        or abs(metrics["split_ratio"] - 0.5) > TOLERANCE
        or metrics["resume_relative_difference"] > TOLERANCE
    ):
        raise ValueError("historical numerical tolerance failed")
    old_mask = json.loads(paths["mask"].read_text())
    old_resume = json.loads(paths["resume"].read_text())
    if (
        old_mask["mask_passed"] is not True
        or old_mask["reduction"] != "sample_mean"
        or old_resume["passed"] is not True
        or old_resume["threshold"] != TOLERANCE
    ):
        raise ValueError("historical result contract changed")
    expected = {
        "duplicate_ratio": old_mask["duplicate_ratio"],
        "split_ratio": old_mask["split_ratio"],
        "resume_relative_difference": old_resume["relative_difference"],
    }
    if any(
        not math.isclose(metrics[k], v, rel_tol=1e-8, abs_tol=1e-12) for k, v in expected.items()
    ):
        raise ValueError("historical weight/result mismatch")
    return kernels, metrics


def review_components(root: Path, output: Path) -> ComponentEvidence:
    """Create new derived evidence, leaving all historical results and weights untouched."""
    inputs = {
        key: FilePin(path=str(root / value), sha256=digest(root / value))
        for key, value in _FILES.items()
    }
    kernels, metrics = _inspect(inputs)
    evidence = ComponentEvidence(kernels=kernels, inputs=inputs, metrics=metrics)
    atomic_record(output, evidence, create=True)
    return evidence


def verify_components(path: Path) -> ComponentEvidence:
    evidence = ComponentEvidence.model_validate_json(path.read_bytes())
    kernels, metrics = _inspect(evidence.inputs)
    if (
        evidence.kernels != kernels
        or set(evidence.metrics) != set(metrics)
        or any(
            not math.isclose(evidence.metrics[k], v, rel_tol=1e-8, abs_tol=1e-12)
            for k, v in metrics.items()
        )
    ):
        raise ValueError("derived component proof changed")
    return evidence

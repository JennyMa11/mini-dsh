"""Fresh initial-adapter structure gate; downloads only after whole-stage admission."""

import json
import tarfile
import zipfile
from decimal import Decimal
from pathlib import Path

import numpy as np

from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest
from minidsh_rl.learning_sdk import LearningSDKRuntime
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.weight_probe import tensor_arrays, tensor_blobs

INITIAL_AUDIT_COST = Decimal(".02")
_CONFIG_KEYS = ("r", "lora_alpha", "lora_dropout", "bias", "target_modules")


def _config(path: Path) -> dict[str, object]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith("adapter_config.json")]
            if len(names) != 1:
                raise ValueError("initial adapter config missing")
            raw = archive.read(names[0])
    else:
        with tarfile.open(path) as archive:
            members = [
                m
                for m in archive.getmembers()
                if m.isfile() and m.name.endswith("adapter_config.json")
            ]
            if len(members) != 1:
                raise ValueError("initial adapter config missing")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError("initial adapter config missing")
            raw = stream.read()
    value = json.loads(raw)
    return {key: value[key] for key in _CONFIG_KEYS}


def _tensors(path: Path) -> tuple[dict[str, tuple[int, ...]], int, float]:
    shapes = {}
    count, squared = 0, 0.0
    for raw in tensor_blobs(path).values():
        for name, value in tensor_arrays(raw).items():
            if not np.isfinite(value).all():
                raise ValueError("nonfinite initial adapter")
            if name in shapes:
                raise ValueError("duplicate initial adapter tensor name")
            shapes[name] = tuple(value.shape)
            if "lora_B" in name:
                count += 1
                squared += float(np.square(value.astype(np.float64)).sum())
    return shapes, count, squared**0.5


def validate_initial_adapter(reference: Path, candidate: Path) -> dict[str, object]:
    config = _config(candidate)
    left, _, _ = _tensors(reference)
    right, count, norm = _tensors(candidate)
    if not left or left != right or config != _config(reference):
        raise ValueError("initial adapter structure changed")
    if not count or norm != 0:
        raise ValueError("initial adapter must have zero LoRA B matrices")
    return {"tensor_count": len(right), "b_matrix_count": count, "b_norm": norm, "config": config}


class InitialAudit(FrozenRecord):
    checkpoint: Checkpoint
    reference_sha256: str
    archive_sha256: str
    metrics: dict[str, object]
    preparation_cny: Decimal


def check_initial(
    runtime: LearningSDKRuntime, checkpoint: Checkpoint, output: Path, reference: Path
) -> Decimal:
    """Read owned checkpoint only; compound failures remain financially pending."""
    client = runtime.client
    if client is None:
        raise ValueError("initial audit requires a training client")
    trainer = TrainerV2(client, runtime.budget, output, "sample_mean")
    candidate = output / "initial.archive"

    def download() -> None:
        rest = runtime.service.create_rest_client()
        response = rest.list_checkpoints(client.training_run_cuid).result()
        matches = [c for c in response.checkpoints if c.path == checkpoint.sampler_uri]
        if len(matches) != 1:
            raise ValueError("owned initial sampler checkpoint missing")
        rest.download_checkpoint(matches[0].checkpoint_id, candidate, resume=False).result()

    trainer.charge("initial_adapter_audit", INITIAL_AUDIT_COST, download)
    metrics = validate_initial_adapter(reference, candidate)
    audited_checkpoint = Checkpoint.model_validate(
        checkpoint.model_dump() | {"preparation_spent": INITIAL_AUDIT_COST}
    )
    atomic_record(
        output / "initial-lora-audit.json",
        InitialAudit(
            checkpoint=audited_checkpoint,
            reference_sha256=digest(reference),
            archive_sha256=digest(candidate),
            metrics=metrics,
            preparation_cny=INITIAL_AUDIT_COST,
        ),
        create=True,
    )
    return INITIAL_AUDIT_COST

"""Paid, bounded custom-loss reduction probe using actual exported LoRA deltas."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]
import torch
from pytrio.types.forward.datum import Datum  # type: ignore[import-untyped]

from minidsh.domain import Message, ModelRequest, Role
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.cost_store import CostStore
from minidsh_rl.gspo import GSPOConfig, build_gspo_batch, make_loss
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.reprice import milli_upper
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.weight_probe import weight_delta

RATIO_TOLERANCE = 0.03


def verify_remote_reduction(output: Path, budget: CostStore) -> dict[str, object]:
    """Compare one vs two identical Datums under linearized optimizer updates.

    Initial policy, optimizer state, sampling and target tokens are identical.
    No unknown operation can be replayed. Exports remain local, never SwanLab.
    """
    output.mkdir(parents=True, exist_ok=False)
    service = trio.ServiceClient()
    operation = (
        output.name + "_create_" + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16]
    )
    budget.reserve(operation, Decimal(".01"))
    initial = service.create_lora_training_client(
        base_model="Qwen/Qwen3.5-4B", rank=16, seed=17, train_unembed=False
    )
    budget.settle(operation, Decimal(".01"))
    base = TrainerV2(initial, budget, output, "sample_mean")
    state = base.checkpoint("numeric_initial")
    rest = service.create_rest_client()

    def export(trainer: TrainerV2, name: str) -> Path:
        saved = trainer.charge(
            name + "_export",
            Decimal(".10"),
            lambda: trainer.client.save_weights_for_sampler(name).result(),
        )
        (output / (name + "_weights.json")).write_text(saved.model_dump_json(indent=2))
        checkpoints = rest.list_checkpoints(trainer.client.training_run_cuid).result()
        cid = next(c.checkpoint_id for c in checkpoints.checkpoints if c.path == saved.path)
        path = output / (name + ".archive")
        rest.download_checkpoint(cid, path, resume=False).result()
        return path

    before = export(base, "numeric_before")
    sampler = service.create_sampling_client(
        base_model="Qwen/Qwen3.5-4B",
        model_path=json.loads((output / "numeric_before_weights.json").read_text())["path"],
    )
    # This probe uses synchronous SDK calls outside any running asyncio event loop.
    import asyncio  # noqa: PLC0415 - local probe driver

    backend = PricedBackend(
        sampler, cast(Tokenizer, initial.get_tokenizer()), budget, "numeric_initial"
    )
    prompt = backend.render(ModelRequest((Message(Role.USER, "Reply with exactly OK."),)))
    sampled = asyncio.run(backend.sample(prompt, 8, 17))
    deltas = {}
    masks = []
    for copies in (1, 2):
        name = f"numeric_copies_{copies}"
        client = base.charge(
            name + "_restore",
            Decimal(".01"),
            lambda: service.create_training_client_from_state_with_optimizer(state),
        )
        trainer = TrainerV2(client, budget, output, "sample_mean")
        batch = build_gspo_batch(((sampled.turn,),) * copies, (1.0,) * copies)
        objective = make_loss(batch, GSPOConfig(0.2, 0.2), remote_sample_mean=False)

        def checked_loss(
            data: list[Datum], values: list[torch.Tensor]
        ) -> tuple[torch.Tensor, dict[str, float]]:
            loss, metrics = objective(data, values)  # noqa: B023 - synchronous call
            grads = torch.autograd.grad(loss, values, retain_graph=True)
            prefix = len(sampled.turn.prompt) - 1
            mask_ok = all(bool((gradient.reshape(-1)[:prefix] == 0).all()) for gradient in grads)
            masks.append(mask_ok)
            if not mask_ok:
                raise ValueError("nonzero observation/prefix gradient")
            return loss, metrics

        response = trainer.charge(
            name + "_fb",
            2 * milli_upper(0, 0, batch.padded_tokens),
            lambda: client.forward_backward_custom(list(batch.datums), checked_loss).result(),  # noqa: B023 - synchronous
        )
        (output / (name + "_fb.json")).write_text(response.model_dump_json(indent=2))
        trainer.charge(
            name + "_optim",
            Decimal(".01"),
            lambda: client.optim_step(  # noqa: B023 - synchronous
                trio.AdamParams(
                    learning_rate=0.001,
                    beta1=0.0,
                    beta2=0.0,
                    eps=1000.0,
                    grad_clip_norm=1e9,
                    weight_decay=0.0,
                )
            ).result(),
        )
        deltas[copies] = weight_delta(before, export(trainer, name))
    ratio = deltas[2] / deltas[1] if deltas[1] > 0 else None
    result = {
        "schema_version": 1,
        "mask_passed": bool(masks) and all(masks),
        "weight_deltas": deltas,
        "duplicate_to_single_delta_ratio": ratio,
        "relative_tolerance": RATIO_TOLERANCE,
        "reduction": "sum",
        "passed": bool(masks)
        and all(masks)
        and ratio is not None
        and abs(ratio - 1) <= RATIO_TOLERANCE,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2))
    if result["passed"] is not True:
        raise ValueError("custom-loss gradient reduction probe failed")
    return result

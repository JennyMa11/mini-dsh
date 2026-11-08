"""Budgeted GSPO custom-loss updates; retain the existing transaction contract."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]
import torch
from pytrio.types.forward import forward_backward_output as fb  # type: ignore[import-untyped]
from pytrio.types.forward.datum import Datum  # type: ignore[import-untyped]
from pytrio.types.optim_step import optim_step_response as opt  # type: ignore[import-untyped]

from minidsh_rl.gspo import GSPOBatch, GSPOConfig, make_loss
from minidsh_rl.reprice import milli_upper
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal

MAX_FRESH_CLIPPED = 0.05


@dataclass(slots=True)
class GSPOTrainer:
    """Custom forward + backward are both reserved; unknown submissions never retry."""

    base: TrainerV2
    config: GSPOConfig

    def update(self, slot: int, batch: GSPOBatch) -> dict[str, object]:
        serialized = json.dumps(
            {
                "datums": [d.model_dump(mode="json") for d in batch.datums],
                "trajectories": [asdict(t) for t in batch.trajectories],
                "config": asdict(self.config),
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        path = self.base.directory / f"update_{slot:03}.json"
        journal = UpdateJournal.create(path, digest)
        if not batch.has_signal:
            journal.advance("skipped")
            return {"updated": False, "datum_hash": digest, "metrics": {}}
        if self.base.reduction != "sum":
            raise ValueError("custom loss requires measured cross_entropy sum reduction")
        objective = make_loss(batch, self.config, remote_sample_mean=False)

        def guarded_loss(
            data: list[Datum], values: list[torch.Tensor]
        ) -> tuple[torch.Tensor, dict[str, float]]:
            loss, metrics = objective(data, values)
            # Observe paper's very narrow clipping without applying it in this wide-clip smoke.
            _, paper = make_loss(batch, GSPOConfig())(data, values)
            metrics["gspo/paper_clip_fraction"] = paper["gspo/active_clip_fraction"]
            raw = [[float(x) for x in value.detach().flatten()] for value in values]
            (self.base.directory / f"current_logprobs_{slot:03}.json").write_text(json.dumps(raw))
            (self.base.directory / f"custom_metrics_{slot:03}.json").write_text(json.dumps(metrics))
            if metrics["gspo/active_clip_fraction"] > MAX_FRESH_CLIPPED:
                raise ValueError("fresh-policy sequence clipping exceeds predeclared 5% gate")
            return loss, metrics

        def backward() -> fb.ForwardBackwardOutput:
            journal.advance("backward_submitted")
            return cast(
                fb.ForwardBackwardOutput,
                self.base.client.forward_backward_custom(list(batch.datums), guarded_loss).result(),
            )

        # SDK 0.2.9 does a forward, then a forward_backward surrogate. Use two full
        # training-token quotes rather than assume the preliminary forward is free.
        result = self.base.charge(
            f"custom_fb_{slot:03}", 2 * milli_upper(0, 0, batch.padded_tokens), backward
        )
        (self.base.directory / f"forward_{slot:03}.json").write_text(
            result.model_dump_json(indent=2)
        )
        if not all(math.isfinite(float(x)) for x in result.metrics.values()):
            raise ValueError("nonfinite custom training output")
        journal.advance("backward_confirmed")

        def optimize() -> opt.OptimStepResponse:
            journal.advance("optim_submitted")
            return cast(
                opt.OptimStepResponse,
                self.base.client.optim_step(
                    trio.AdamParams(
                        learning_rate=1e-5,
                        beta1=0.9,
                        beta2=0.95,
                        grad_clip_norm=1.0,
                        weight_decay=0.0,
                    )
                ).result(),
            )

        optimized = self.base.charge(f"optim_{slot:03}", Decimal(".01"), optimize)
        (self.base.directory / f"optim_{slot:03}.json").write_text(
            optimized.model_dump_json(indent=2)
        )
        journal.advance("optim_confirmed")
        metrics = json.loads((self.base.directory / f"custom_metrics_{slot:03}.json").read_text())
        return {"updated": True, "datum_hash": digest, "metrics": metrics}

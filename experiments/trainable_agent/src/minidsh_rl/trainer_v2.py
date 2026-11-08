"""Reuse measured normalization/PPO diagnostics with the durable financial port."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, TypeVar, cast

import pytrio as trio  # type: ignore[import-untyped]
from pytrio.client.training import TrainingClient  # type: ignore[import-untyped]
from pytrio.types.forward import forward_backward_output as fb  # type: ignore[import-untyped]
from pytrio.types.optim_step import optim_step_response as opt  # type: ignore[import-untyped]

from minidsh_rl.datum import TrainingBatch
from minidsh_rl.reprice import milli_upper
from minidsh_rl.trainer import CLIP, MAX_CLIPPED_SHARE, normalized_batch, policy_diagnostics
from minidsh_rl.transaction import UpdateJournal

_T = TypeVar("_T")


class BudgetPort(Protocol):
    @property
    def available(self) -> Decimal: ...
    def reserve(self, operation: str, maximum: Decimal) -> None: ...
    def settle(self, operation: str, actual: Decimal) -> None: ...


@dataclass(slots=True)
class TrainerV2:
    """Explicit SDK client, artifact directory and shared cumulative spending lease."""

    client: TrainingClient
    budget: BudgetPort
    directory: Path
    reduction: str
    evaluation_reserve: Decimal = Decimal("0")

    def charge(self, name: str, maximum: Decimal, call: Callable[[], _T]) -> _T:
        """Unknown RPC failures retain their reservation and are not auto-retried."""
        if self.budget.available - maximum < self.evaluation_reserve:
            raise ValueError("evaluation reserve would be consumed")
        scope = hashlib.sha256(str(self.directory.resolve()).encode()).hexdigest()[:16]
        operation = self.directory.name + "_" + scope + "_" + name
        self.budget.reserve(operation, maximum)
        result = call()
        self.budget.settle(operation, maximum)
        return result

    def snapshot(self, name: str) -> object:
        """Freeze sampler weights before the next complete group is generated."""
        sampler = self.charge(
            name, Decimal(".10"), self.client.save_weights_and_get_sampling_client
        )
        (self.directory / (name + ".json")).write_text(
            json.dumps(
                {
                    "sampling_run_cuid": sampler.sampling_run_cuid,
                    "training_run_cuid": self.client.training_run_cuid,
                },
                indent=2,
            )
        )
        return sampler

    def checkpoint(self, name: str) -> str:
        """Train state, including the optimizer; never overwrite a prior checkpoint."""
        state = self.charge(name, Decimal(".10"), lambda: self.client.save_state(name).result())
        (self.directory / (name + ".json")).write_text(state.model_dump_json(indent=2))
        return str(state.path)

    def update(self, slot: int, batch: TrainingBatch) -> dict[str, object]:
        """One batch, one epoch and one optim_step; full zero-signal groups skip."""
        batch = normalized_batch(batch, self.reduction)
        serialized = json.dumps([d.model_dump(mode="json") for d in batch.datums], sort_keys=True)
        datum_hash = hashlib.sha256(serialized.encode()).hexdigest()
        journal = UpdateJournal.create(self.directory / f"update_{slot:03}.json", datum_hash)
        if not batch.has_signal:
            journal.advance("skipped")
            return {"slot": slot, "updated": False, "datum_hash": datum_hash}

        def backward() -> fb.ForwardBackwardOutput:
            journal.advance("backward_submitted")
            return cast(
                fb.ForwardBackwardOutput,
                self.client.forward_backward(
                    list(batch.datums), loss_fn="ppo", loss_fn_config=CLIP, auto_shift=False
                ).result(),
            )

        result = self.charge(
            f"fb_{slot:03}",
            milli_upper(0, 0, batch.padded_tokens),
            backward,
        )
        (self.directory / f"forward_{slot:03}.json").write_text(result.model_dump_json(indent=2))
        if not all(math.isfinite(float(x)) for x in result.metrics.values()):
            raise ValueError("nonfinite training metrics")
        diagnostics = policy_diagnostics(
            batch,
            tuple(
                tuple(float(x) for x in item["logprobs"].data) for item in result.loss_fn_outputs
            ),
        )
        (self.directory / f"policy_{slot:03}.json").write_text(json.dumps(diagnostics, indent=2))
        if diagnostics["clipped_share"] > MAX_CLIPPED_SHARE:
            raise ValueError("fresh policy probability mismatch exceeds clip safety gate")
        journal.advance("backward_confirmed")

        def optimize() -> opt.OptimStepResponse:
            journal.advance("optim_submitted")
            return cast(
                opt.OptimStepResponse,
                self.client.optim_step(
                    trio.AdamParams(
                        learning_rate=1e-5,
                        beta1=0.9,
                        beta2=0.95,
                        grad_clip_norm=1.0,
                        weight_decay=0.0,
                    )
                ).result(),
            )

        optimized = self.charge(
            f"optim_{slot:03}",
            Decimal(".01"),
            optimize,
        )
        (self.directory / f"optim_{slot:03}.json").write_text(optimized.model_dump_json(indent=2))
        journal.advance("optim_confirmed")
        return {
            "slot": slot,
            "updated": True,
            "datum_hash": datum_hash,
            "compute_tokens": batch.compute_tokens,
            "assistant_tokens": batch.assistant_tokens,
            "metrics": {str(k): float(v) for k, v in result.metrics.items()},
        }

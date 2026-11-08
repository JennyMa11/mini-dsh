"""One on-policy logical PPO update; all numerical compute remains in PyTRIO."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import pytrio as trio  # type: ignore[import-untyped]
from pytrio.client.training import TrainingClient  # type: ignore[import-untyped]

from minidsh_rl.budget import BudgetStore, cost_upper_bound
from minidsh_rl.datum import TrainingBatch
from minidsh_rl.transaction import UpdateJournal

_T = TypeVar("_T")
CLIP = {"clip_low_threshold": 0.8, "clip_high_threshold": 1.2}


def normalized_batch(batch: TrainingBatch, reduction: str) -> TrainingBatch:
    """Correct only a measured backend reduction, without mutating trajectory data."""
    if reduction == "assistant_token_mean":
        return batch
    if reduction == "sample_mean":
        scale = len(batch.datums) / batch.assistant_tokens
    elif reduction == "sum":
        scale = 1 / batch.assistant_tokens
    else:
        raise ValueError("backend reduction has not passed its numerical gate")
    datums = []
    for datum in batch.datums:
        data = datum.model_dump(mode="json")
        values = [float(x) * scale for x in datum.loss_fn_inputs["advantages"].data]
        data["loss_fn_inputs"]["advantages"] = {
            "data": values,
            "shape": [len(values)],
            "dtype": "float32",
        }
        datums.append(trio.Datum.model_validate(data))
    return TrainingBatch(
        tuple(datums),
        batch.assistant_tokens,
        batch.compute_tokens,
        batch.padded_tokens,
        batch.has_signal,
    )


MAX_CLIPPED_SHARE = 0.05


def policy_diagnostics(
    batch: TrainingBatch, logs: tuple[tuple[float, ...], ...]
) -> dict[str, float]:
    clipped = checked = 0
    total_delta = maximum = 0.0
    for datum, actual in zip(batch.datums, logs, strict=True):
        old = datum.loss_fn_inputs["logprobs"].data
        adv = datum.loss_fn_inputs["advantages"].data
        for target, sampled, weight in zip(actual, old, adv, strict=True):
            if not weight:
                continue
            if not math.isfinite(target):
                raise ValueError("nonfinite policy probability")
            delta = target - sampled
            maximum = max(maximum, abs(delta))
            total_delta += abs(delta)
            checked += 1
            clipped += not (math.log(0.8) <= delta <= math.log(1.2))
    if not checked:
        raise ValueError("no measured action probabilities")
    return {
        "clipped_share": clipped / checked,
        "action_tokens": float(checked),
        "mean_abs_log_ratio": total_delta / checked,
        "max_abs_log_ratio": maximum,
    }


@dataclass(slots=True)
class Trainer:
    """Explicit SDK client, artifact directory and shared cumulative spending lease."""

    client: TrainingClient
    budget: BudgetStore
    directory: Path
    reduction: str

    def charge(self, name: str, maximum: Decimal, call: Callable[[], _T]) -> _T:
        """Unknown RPC failures retain their reservation and are not auto-retried."""
        if self.budget.ledger.available - maximum < Decimal("15"):
            raise ValueError("evaluation reserve would be consumed")
        operation = self.directory.name + "_" + name
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
        journal.advance("backward_submitted")
        result = self.charge(
            f"fb_{slot:03}",
            cost_upper_bound(0, 0, batch.padded_tokens),
            lambda: self.client.forward_backward(
                list(batch.datums), loss_fn="ppo", loss_fn_config=CLIP, auto_shift=False
            ).result(),
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
        journal.advance("optim_submitted")
        optimized = self.charge(
            f"optim_{slot:03}",
            Decimal(".01"),
            lambda: self.client.optim_step(
                trio.AdamParams(
                    learning_rate=1e-5, beta1=0.9, beta2=0.95, grad_clip_norm=1.0, weight_decay=0.0
                )
            ).result(),
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

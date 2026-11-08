"""Multi-turn GSPO, adapted from Agentic RL Lab's sequence-level objective.

Reference: datawhalechina/agentic-rl-lab@238bc688, experiments/gspo/loss.py
Changes: each sequence is a full agent trajectory; observations stay masked;
group-centred advantages and optional measured remote sample-mean correction.
This is a coding-task adaptation, not a reproduction of the Lab math run.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

import torch
from pytrio.types.forward.datum import Datum  # type: ignore[import-untyped]

from minidsh_rl.contracts import Turn
from minidsh_rl.datum import build_batch


@dataclass(frozen=True, slots=True)
class GSPOConfig:
    """Lab paper defaults; a different clip must be explicit in experiment config."""

    clip_low: float = 0.0003
    clip_high: float = 0.0004

    def __post_init__(self) -> None:
        if not all(math.isfinite(x) and 0 <= x < 1 for x in (self.clip_low, self.clip_high)):
            raise ValueError("invalid GSPO clipping bounds")


@dataclass(frozen=True, slots=True)
class TrajectoryMeta:
    """Indices and lengths refer to per-call Datums with their actual prefixes."""

    indices: tuple[int, ...]
    sampling: tuple[tuple[float, ...], ...]
    advantage: float


@dataclass(frozen=True, slots=True)
class GSPOBatch:
    """All trajectories, including zero-signal groups; never silently drop a run."""

    datums: tuple[Datum, ...]
    trajectories: tuple[TrajectoryMeta, ...]
    padded_tokens: int
    assistant_tokens: int
    has_signal: bool


def build_gspo_batch(
    trajectories: tuple[tuple[Turn, ...], ...], advantages: tuple[float, ...]
) -> GSPOBatch:
    """Reuse existing right-shift/mask contracts without re-tokenizing actions."""
    if not trajectories or any(not turns for turns in trajectories):
        raise ValueError("every sampled trajectory must contain recorded turns")
    batch = build_batch(trajectories, advantages)
    offset = 0
    metas = []
    for turns, advantage in zip(trajectories, advantages, strict=True):
        indices = tuple(range(offset, offset + len(turns)))
        metas.append(TrajectoryMeta(indices, tuple(t.logprobs for t in turns), advantage))
        offset += len(turns)
    return GSPOBatch(
        batch.datums, tuple(metas), batch.padded_tokens, batch.assistant_tokens, batch.has_signal
    )


LossFn = Callable[[list[Datum], list[torch.Tensor]], tuple[torch.Tensor, dict[str, float]]]


def make_loss(batch: GSPOBatch, config: GSPOConfig, *, remote_sample_mean: bool = False) -> LossFn:
    """Equal trajectory mean, assistant-only geometric ratio, one clip per task.

    PyTRIO 0.2.9 computes local d(loss)/d(logprob), then sends negative gradients
    as cross_entropy weights. If the backend averages Datums, multiply only the
    returned surrogate by their count; diagnostics retain the mathematical loss.
    """

    def loss_fn(
        data: list[Datum], values: list[torch.Tensor]
    ) -> tuple[torch.Tensor, dict[str, float]]:
        if len(data) != len(batch.datums) or len(values) != len(data):
            raise ValueError("GSPO callback batch mismatch")
        objectives, ratios, clipped = [], [], []
        for meta in batch.trajectories:
            differences = []
            for index, old in zip(meta.indices, meta.sampling, strict=True):
                current = values[index].float().reshape(-1)
                expected = batch.datums[index].model_input.length
                if current.numel() != expected or not bool(torch.isfinite(current).all()):
                    raise ValueError("invalid current logprob shape or values")
                sampled = torch.tensor(old, dtype=current.dtype, device=current.device)
                differences.append(current[-len(old) :] - sampled)
            ratio = torch.exp(torch.cat(differences).mean())
            if not bool(torch.isfinite(ratio)):
                raise ValueError("nonfinite sequence importance ratio")
            bounded = torch.clamp(ratio, 1 - config.clip_low, 1 + config.clip_high)
            raw, limited = ratio * meta.advantage, bounded * meta.advantage
            objectives.append(torch.minimum(raw, limited))
            ratios.append(float(ratio.detach()))
            clipped.append(float(limited.detach() < raw.detach()))
        objective = -torch.stack(objectives).mean()
        metrics = {
            "gspo/loss": float(objective.detach()),
            "gspo/ratio_mean": sum(ratios) / len(ratios),
            "gspo/ratio_min": min(ratios),
            "gspo/ratio_max": max(ratios),
            "gspo/active_clip_fraction": sum(clipped) / len(clipped),
            "gspo/trajectories": float(len(batch.trajectories)),
            "gspo/datums": float(len(data)),
            "gspo/assistant_tokens": float(batch.assistant_tokens),
            "gspo/remote_scale": float(len(data) if remote_sample_mean else 1),
        }
        return objective * metrics["gspo/remote_scale"], metrics

    return loss_fn

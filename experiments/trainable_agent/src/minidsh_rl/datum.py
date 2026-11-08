"""Exact per-call RL Datums; no re-tokenization of generated actions."""

from dataclasses import dataclass

import pytrio as trio  # type: ignore[import-untyped]
from pytrio.types.forward.datum import Datum  # type: ignore[import-untyped]

from minidsh_rl.contracts import Turn, training_arrays


@dataclass(frozen=True, slots=True)
class TrainingBatch:
    """One logical batch, retaining zero-signal trajectories and real lengths."""

    datums: tuple[Datum, ...]
    assistant_tokens: int
    compute_tokens: int
    padded_tokens: int
    has_signal: bool


def build_batch(
    trajectories: tuple[tuple[Turn, ...], ...],
    advantages: tuple[float, ...],
) -> TrainingBatch:
    """Use raw advantages; reduction is verified against the backend before updates."""
    datums = []
    assistant_tokens = compute_tokens = maximum = 0
    has_signal = False
    for turns, advantage in zip(trajectories, advantages, strict=True):
        for turn in turns:
            inputs, targets, logs, adv = training_arrays(turn, advantage)
            datums.append(
                trio.Datum(
                    model_input=trio.ModelInput.from_ints(list(inputs)),
                    loss_fn_inputs={
                        "target_tokens": list(targets),
                        "logprobs": list(logs),
                        "advantages": list(adv),
                    },
                )
            )
            assistant_tokens += len(turn.completion)
            compute_tokens += len(inputs)
            maximum = max(maximum, len(inputs))
            has_signal |= advantage != 0
    if not datums:
        raise ValueError("no assistant tokens to train")
    return TrainingBatch(
        tuple(datums), assistant_tokens, compute_tokens, maximum * len(datums), has_signal
    )

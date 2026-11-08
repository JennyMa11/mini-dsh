"""Training boundary: exact sampled targets and a token-weighted objective."""

from minidsh_rl.contracts import Turn
from minidsh_rl.datum import build_batch


def test_sdk_datum_keeps_shifted_ids_and_masks_history() -> None:
    batch = build_batch(((Turn((10, 11), (12, 13), (-0.2, -0.3)),),), (1.0,))
    assert batch.assistant_tokens == 2
    datum = batch.datums[0]
    assert datum.model_input.to_ints() == [10, 11, 12]
    assert datum.loss_fn_inputs["target_tokens"].data == [11, 12, 13]
    assert datum.loss_fn_inputs["logprobs"].data == [
        0.0,
        -0.20000000298023224,
        -0.30000001192092896,
    ]
    assert datum.loss_fn_inputs["advantages"].data == [0.0, 1.0, 1.0]


def test_zero_reward_groups_retained_with_zero_advantages() -> None:
    turn = Turn((1, 2), (3,), (-0.1,))
    batch = build_batch(((turn,), (turn, turn)), (0.0, 0.0))
    assert len(batch.datums) == 3
    assert batch.assistant_tokens == 3
    assert not batch.has_signal
    assert all(not any(d.loss_fn_inputs["advantages"].data) for d in batch.datums)

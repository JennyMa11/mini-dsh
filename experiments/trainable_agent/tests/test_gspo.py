"""GSPO contracts: entire trajectories, masked observations and remote reduction."""

import math

import pytest

torch = pytest.importorskip("torch")

from minidsh_rl.contracts import Turn  # noqa: E402
from minidsh_rl.gspo import GSPOConfig, build_gspo_batch, make_loss  # noqa: E402


def test_multiturn_ratio_uses_all_assistant_tokens_and_zero_prefix_gradients() -> None:
    turns = (Turn((1, 2), (3,), (-1.0,)), Turn((4, 5, 6), (7, 8), (-2.0, -3.0)))
    batch = build_gspo_batch((turns,), (1.0,))
    values = [
        torch.tensor([-999.0, -0.9], requires_grad=True),
        torch.tensor([-999.0, -999.0, -1.8, -2.7], requires_grad=True),
    ]
    loss, metrics = make_loss(batch, GSPOConfig(0.5, 0.5))(list(batch.datums), values)
    assert metrics["gspo/ratio_mean"] == pytest.approx(math.exp(0.2))
    assert float(loss.detach()) == pytest.approx(-math.exp(0.2))
    loss.backward()  # type: ignore[no-untyped-call]
    assert values[0].grad.tolist() == pytest.approx([0, -math.exp(0.2) / 3])
    assert values[1].grad.tolist() == pytest.approx([0, 0, -math.exp(0.2) / 3, -math.exp(0.2) / 3])


def test_zero_signal_retains_original_trajectory_denominator() -> None:
    turn = Turn((1,), (2,), (-1.0,))
    batch = build_gspo_batch(((turn,), (turn,)), (1.0, 0.0))
    values = [torch.tensor([-1.0], requires_grad=True) for _ in range(2)]
    loss, _ = make_loss(batch, GSPOConfig())(list(batch.datums), values)
    loss.backward()  # type: ignore[no-untyped-call]
    assert float(loss.detach()) == -0.5
    assert values[0].grad.item() == -0.5
    assert values[1].grad.item() == 0


@pytest.mark.parametrize(("advantage", "delta"), [(1.0, 0.5), (-1.0, -0.5)])
def test_clipping_stops_gradient_for_improving_outside_bound(
    advantage: float, delta: float
) -> None:
    batch = build_gspo_batch(((Turn((1,), (2,), (-1.0,)),),), (advantage,))
    values = [torch.tensor([-1.0 + delta], requires_grad=True)]
    loss, metrics = make_loss(batch, GSPOConfig(0.2, 0.2))(list(batch.datums), values)
    loss.backward()  # type: ignore[no-untyped-call]
    assert metrics["gspo/active_clip_fraction"] == 1
    assert values[0].grad.item() == 0


def test_sdk_sample_mean_compensation_is_explicit() -> None:
    turn = Turn((1,), (2,), (-1.0,))
    batch = build_gspo_batch(((turn, turn),), (1.0,))
    values = [torch.tensor([-1.0], requires_grad=True) for _ in range(2)]
    loss, metrics = make_loss(batch, GSPOConfig(), remote_sample_mean=True)(
        list(batch.datums), values
    )
    loss.backward()  # type: ignore[no-untyped-call]
    assert metrics["gspo/loss"] == -1.0
    assert loss.item() == -2.0
    # Remote backend averages the two linear surrogate Datums.
    assert values[0].grad.item() / len(values) == -0.5


def test_reject_nonfinite_and_missing_turns_before_rpc() -> None:
    with pytest.raises(ValueError, match="invalid GSPO"):
        GSPOConfig(float("nan"), 0.1)
    with pytest.raises(ValueError, match="every sampled"):
        build_gspo_batch(((),), (1.0,))
    with pytest.raises(ValueError, match="nonfinite advantage"):
        build_gspo_batch(((Turn((1,), (2,), (-1.0,)),),), (float("nan"),))


def test_ratio_invariant_to_turn_splitting_and_prompt_length() -> None:
    whole = build_gspo_batch(((Turn((1,), (2, 3), (-1.0, -2.0)),),), (1.0,))
    split = build_gspo_batch(((Turn((1,), (2,), (-1.0,)), Turn((1, 9), (3,), (-2.0,))),), (1.0,))
    one, _ = make_loss(whole, GSPOConfig(0.5, 0.5))(
        list(whole.datums), [torch.tensor([-0.9, -1.8], requires_grad=True)]
    )
    two, _ = make_loss(split, GSPOConfig(0.5, 0.5))(
        list(split.datums),
        [torch.tensor([-0.9], requires_grad=True), torch.tensor([99.0, -1.8], requires_grad=True)],
    )
    assert one.item() == pytest.approx(two.item())

"""Cross-boundary token, reward and budget contracts."""

import math
from decimal import Decimal

import pytest
from minidsh_rl.contracts import Ledger, Turn, advantages, training_arrays


def test_multiturn_masks_only_current_actions() -> None:
    first = Turn((10, 11), (12, 13), (-0.2, -0.3))
    second = Turn((10, 11, 12, 13, 90, 91), (14,), (-0.4,))
    a = training_arrays(first, 0.5)
    b = training_arrays(second, 0.5)
    assert a == ((10, 11, 12), (11, 12, 13), (0.0, -0.2, -0.3), (0.0, 0.5, 0.5))
    assert b[3] == (0.0, 0.0, 0.0, 0.0, 0.0, 0.5)
    assert sum(x != 0 for x in (*a[3], *b[3])) == 3


@pytest.mark.parametrize("logs", [(), (math.nan,), (math.inf,)])
def test_missing_or_nonfinite_logprob_rejected(logs: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="logprob"):
        Turn((1,), (2,), logs)


def test_group_reward_preserves_failures_and_zero_signal() -> None:
    assert advantages((1.0, 0.0, 0.0, 1.0)) == (0.5, -0.5, -0.5, 0.5)
    assert advantages((0.0, 0.0, 0.0, 0.0)) == (0.0,) * 4
    with pytest.raises(ValueError, match="invalid reward group"):
        advantages((float("nan"), 0.0))


def test_unknown_and_inflight_cost_remain_reserved() -> None:
    ledger = Ledger(Decimal("50"))
    ledger.reserve("rpc1", Decimal("30"))
    with pytest.raises(ValueError, match="budget exhausted"):
        ledger.reserve("rpc2", Decimal("21"))
    with pytest.raises(ValueError, match="duplicate operation"):
        ledger.reserve("rpc1", Decimal("1"))
    ledger.settle("rpc1", Decimal("12"))
    assert ledger.available == Decimal("38")
    ledger.reserve("timeout", Decimal("38"))
    assert ledger.available == 0
    with pytest.raises(ValueError, match="budget exhausted"):
        ledger.reserve("next", Decimal(".01"))


def test_actual_overrun_is_recorded_and_blocks_next_call() -> None:
    ledger = Ledger(Decimal("1"))
    ledger.reserve("rpc", Decimal("1"))
    ledger.settle("rpc", Decimal("2"))
    assert ledger.spent == Decimal("2")
    assert ledger.available == Decimal("-1")
    with pytest.raises(ValueError, match="budget exhausted"):
        ledger.reserve("next", Decimal(".01"))

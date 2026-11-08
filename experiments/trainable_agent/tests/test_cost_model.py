"""Budget gates count unknown requests and avoid double-counting reconciled usage."""

from decimal import Decimal

import pytest
from minidsh_rl.cost_model import Charge, CostAccount, PriceCard


def account() -> CostAccount:
    return CostAccount(limit=Decimal("50"), historical=Decimal("31.38"), uncertainty=Decimal("2"))


def test_continuous_estimation_keeps_small_request_precision() -> None:
    card = PriceCard(Decimal("1.49"), Decimal("4.54"), Decimal("4.54"))
    assert card.estimate(inputs=1, outputs=1) == Decimal("0.00000603")
    assert card.estimate(inputs=663028, outputs=40733) == Decimal("1.17283954")
    with pytest.raises(ValueError, match="invalid"):
        card.estimate(inputs=-1)


def test_pending_and_actual_overrun_remain_charged() -> None:
    ledger = account()
    ledger.reserve("unknown", Decimal("5"))
    assert ledger.committed == Decimal("38.38")
    with pytest.raises(ValueError, match="budget"):
        ledger.reserve("next", Decimal("12"))
    ledger.settle("unknown", Decimal("20"))
    assert ledger.available == Decimal("-3.38")
    with pytest.raises(ValueError, match="budget"):
        ledger.reserve("blocked", Decimal(".01"))


def test_bill_replaces_estimate_only_for_explicit_operations() -> None:
    ledger = account()
    ledger.reserve("one", Decimal("1"))
    ledger.settle("one", Decimal(".8"))
    ledger.reserve("two", Decimal("1"))
    ledger.settle("two", Decimal(".7"))
    ledger.reconcile("bill_1", ("one",), Decimal(".3"), "a" * 64)
    assert ledger.committed == Decimal("34.38")
    with pytest.raises(ValueError, match="reconciled"):
        ledger.reconcile("bill_2", ("one", "two"), Decimal(".5"), "b" * 64)
    assert ledger.charges["two"] == Charge(Decimal(".7"), "estimated")
    with pytest.raises(ValueError, match="duplicate"):
        ledger.reserve("one", Decimal("1"))


def test_ambiguous_workspace_bill_cannot_release_pending_or_history() -> None:
    ledger = account()
    ledger.reserve("pending", Decimal("1"))
    with pytest.raises(ValueError, match="pending"):
        ledger.reconcile("bill", ("pending",), Decimal(".01"), "a" * 64)
    assert ledger.historical == Decimal("31.38")
    assert ledger.committed == Decimal("34.38")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
def test_nonfinite_or_negative_cost_is_rejected(amount: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        account().reserve("bad", Decimal(amount))


def test_quote_or_bill_identity_cannot_be_ambiguous() -> None:
    ledger = account()
    ledger.reserve("one", Decimal("1"))
    ledger.settle("one", Decimal(".5"))
    for operations, digest in [((), "a" * 64), (("one", "one"), "a" * 64), (("one",), "no-proof")]:
        with pytest.raises(ValueError, match="invalid"):
            ledger.reconcile("bill", operations, Decimal(".3"), digest)
    assert ledger.committed == Decimal("33.88")

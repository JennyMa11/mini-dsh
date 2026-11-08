"""Crash-safe reservations must survive restarts and block duplicate submission."""

from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.budget import BudgetStore, cost_upper_bound


def test_pending_reservation_survives_reopen_and_does_not_allow_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    with BudgetStore(path, Decimal("50")) as store:
        store.reserve("rpc", Decimal("49"))
    with BudgetStore(path, Decimal("50")) as store:
        assert store.ledger.available == Decimal("1")
        with pytest.raises(ValueError, match="duplicate"):
            store.reserve("rpc", Decimal("1"))
        with pytest.raises(ValueError, match="budget exhausted"):
            store.reserve("next", Decimal("2"))
        store.settle("rpc", Decimal("2"))
    with BudgetStore(path, Decimal("50")) as store:
        assert store.ledger.spent == Decimal("2")
        assert store.ledger.available == Decimal("48")


def test_second_writer_rejected_and_limit_cannot_increase(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    with BudgetStore(path, Decimal("50")), pytest.raises(ValueError, match="busy"):
        BudgetStore(path, Decimal("50"))
    with pytest.raises(ValueError, match="limit changed"):
        BudgetStore(path, Decimal("100"))


def test_per_request_rounding_and_training_padding_reserved() -> None:
    assert cost_upper_bound(1, 1, 0) == Decimal(".02")
    assert cost_upper_bound(4096, 512, 0) == Decimal(".02")
    assert cost_upper_bound(0, 0, 32768) == Decimal(".15")

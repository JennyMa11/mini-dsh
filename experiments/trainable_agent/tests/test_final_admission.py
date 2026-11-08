"""Final test admission preserves complete denominator and the cumulative cap."""

from decimal import Decimal

import pytest
from minidsh_rl.final_admission import resource_admission


def test_exact_full_test_reservation_is_sufficient() -> None:
    assert resource_admission(
        slots=40,
        validation_runs=64,
        audited_slots=40,
        nonzero_updates=12,
        pending=False,
        available=Decimal("7.68"),
    )


@pytest.mark.parametrize(
    "case",
    [
        (39, 64, 39, 12, False, "50"),
        (40, 63, 40, 12, False, "50"),
        (40, 64, 39, 12, False, "50"),
        (40, 64, 40, 0, False, "50"),
        (40, 64, 40, 12, True, "50"),
        (40, 64, 40, 12, False, "7.679"),
    ],
)
def test_incomplete_unknown_or_underfunded_test_stays_closed(
    case: tuple[int, int, int, int, bool, str],
) -> None:
    slots, verified, audited, updates, pending, available = case
    assert not resource_admission(
        slots=slots,
        validation_runs=verified,
        audited_slots=audited,
        nonzero_updates=updates,
        pending=pending,
        available=Decimal(available),
    )

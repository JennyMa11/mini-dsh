"""Published cents-to-CNY conversion and full-queue arithmetic."""

from decimal import Decimal

from minidsh_rl.preflight import estimate_cny


def test_published_rates_convert_from_cents_to_cny() -> None:
    assert estimate_cny(1000000, 0, 0) == Decimal("1.49")
    assert estimate_cny(0, 1000000, 0) == Decimal("4.54")
    assert estimate_cny(0, 0, 1000000) == Decimal("4.54")
    assert estimate_cny(1056000, 176000, 616000) == Decimal("5.16912")

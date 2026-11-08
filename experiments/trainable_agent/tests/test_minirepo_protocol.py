"""Freeze a complete alternating paired queue and prove its monetary admission."""

from decimal import Decimal

from minidsh_rl.minirepo_experiment import RUN_BOUND, evaluation_queue, run_cost_bound


def test_queue_pairs_same_case_seed_and_alternates_modes() -> None:
    rows = evaluation_queue()
    assert len(rows) == 32
    for i in range(0, len(rows), 2):
        left, right = rows[i : i + 2]
        assert (left["case_id"], left["seed"]) == (right["case_id"], right["seed"])
        assert {left["mode"], right["mode"]} == {"single", "interactive"}
        assert left["mode"] == ("single" if (i // 2) % 2 == 0 else "interactive")
    assert len({(r["case_id"], r["seed"], r["mode"]) for r in rows}) == 32


def test_reservation_bounds_all_inflight_usage_and_rounding() -> None:
    assert run_cost_bound() < RUN_BOUND
    assert Decimal("2.88") == 32 * RUN_BOUND

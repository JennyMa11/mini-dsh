"""Freeze the staged allocation decision before observing trained validation."""

from minidsh_rl.analyze_v2 import extension_rule


def test_positive_count_or_efficiency_requires_sufficient_effective_updates() -> None:
    base: dict[str, object] = {"successes": 20, "tokens_per_success": 15000}
    rl: dict[str, object] = {"successes": 22, "tokens_per_success": 16000}
    assert extension_rule(base, rl, effective=8, end_slot=40)["allowed_end_slot"] == 120
    assert extension_rule(base, rl, effective=7, end_slot=40)["allow_extension"] is False
    efficient: dict[str, object] = {"successes": 20, "tokens_per_success": 13500}
    assert extension_rule(base, efficient, effective=8, end_slot=40)["directional_signal"] is True
    regressed: dict[str, object] = {"successes": 19, "tokens_per_success": 10000}
    assert extension_rule(base, regressed, effective=8, end_slot=40)["allow_extension"] is False


def test_no_signal_gets_only_one_predeclared_60_slot_probe() -> None:
    base: dict[str, object] = {"successes": 20, "tokens_per_success": 15000}
    rl: dict[str, object] = {"successes": 20, "tokens_per_success": 15000}
    result = extension_rule(base, rl, effective=12, end_slot=40)
    assert result["allowed_end_slot"] == 60
    assert result["directional_signal"] is False
    assert result["allow_final_test"] is False
    assert extension_rule(base, rl, effective=12, end_slot=60)["allow_extension"] is False
    assert extension_rule(base, rl, effective=11, end_slot=40)["allow_extension"] is False


def test_no_success_does_not_invent_token_per_success_improvement() -> None:
    absent: dict[str, object] = {"successes": 0, "tokens_per_success": None}
    assert extension_rule(absent, absent, effective=8, end_slot=40)["directional_signal"] is False

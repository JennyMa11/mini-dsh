"""Unrun checkpoints remain unknown; paired effects require complete evidence."""

from minidsh_rl.curve_analysis import bootstrap_delta, checkpoint_metrics


def test_unrun_is_not_zero_success_rate() -> None:
    report = checkpoint_metrics([])
    assert report["declared"] == 24
    assert report["completed"] == 0
    assert report["success_rate_checked"] is None
    assert report["tokens_per_success"] is None


def test_family_bootstrap_preserves_signed_effect() -> None:
    assert bootstrap_delta({"a": [1, 1], "b": [1, 1], "c": [1, 1]}) == [1, 1]
    assert bootstrap_delta({"a": [-1, -1], "b": [-1, -1], "c": [-1, -1]}) == [-1, -1]

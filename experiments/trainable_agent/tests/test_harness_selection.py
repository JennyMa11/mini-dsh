"""Selection cannot reinterpret an incomplete queue or accept a success regression."""

from minidsh_rl.analyze_harness import choose


def test_incomplete_or_regressing_candidate_cannot_be_adopted() -> None:
    assert (
        choose(
            complete=False,
            original_success=10,
            candidate_success=15,
            original_rejected=10,
            original_calls=100,
            candidate_rejected=0,
            candidate_calls=100,
        )
        is None
    )
    assert (
        choose(
            complete=True,
            original_success=10,
            candidate_success=9,
            original_rejected=10,
            original_calls=100,
            candidate_rejected=0,
            candidate_calls=100,
        )
        == "original"
    )


def test_predeclared_rejection_rate_threshold_is_applied() -> None:
    assert (
        choose(
            complete=True,
            original_success=10,
            candidate_success=10,
            original_rejected=10,
            original_calls=100,
            candidate_rejected=3,
            candidate_calls=50,
        )
        == "json_newlines"
    )
    assert (
        choose(
            complete=True,
            original_success=10,
            candidate_success=10,
            original_rejected=0,
            original_calls=100,
            candidate_rejected=0,
            candidate_calls=50,
        )
        == "original"
    )

"""Fixed attempted-slot curve and readiness decisions precede provider execution."""

from collections import Counter

from minidsh_rl.curve_schedule import make_curve_plan, readiness_gate


def test_full_frozen_queue_and_checkpoints() -> None:
    plan = make_curve_plan()
    assert len(plan.rows) == 216
    assert Counter(r.phase for r in plan.rows) == {"dev": 24, "train": 96, "validation": 96}
    assert len({r.run_id for r in plan.rows}) == 216
    assert {r.checkpoint for r in plan.rows if r.phase == "validation"} == {0, 8, 16, 24}
    assert set(Counter(r.case_id for r in plan.rows if r.phase == "train").values()) == {8}
    assert make_curve_plan() == plan
    assert 0 < plan.required_cny < 100
    for case in {r.case_id for r in plan.rows if r.phase == "validation"}:
        rows = [r for r in plan.rows if r.case_id == case]
        assert len({r.seed for r in rows}) == 2
        assert all(sum(r.checkpoint == k for r in rows) == 2 for k in (0, 8, 16, 24))


def test_readiness_requires_mixture_and_two_feedback_groups() -> None:
    rewards = [[True, False, False, False]] * 6
    families = ["a", "a", "b", "b", "c", "c"]
    assert readiness_gate(rewards, families, {"a", "b"})
    assert not readiness_gate(rewards, families, {"a"})
    assert not readiness_gate([[False] * 4] * 6, families, {"a", "b"})
    assert not readiness_gate(rewards[:3], families[:3], {"a", "b"})

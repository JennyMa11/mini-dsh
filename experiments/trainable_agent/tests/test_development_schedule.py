"""Single fixed admission queue; future training cannot be selected by this entry."""

from minidsh_rl.development_schedule import make_development_plan


def test_queue_reuses_declared_seed_pairs_and_never_contains_training() -> None:
    plan = make_development_plan()
    assert len(plan.rows) == 24
    assert len({r.run_id for r in plan.rows}) == 24
    assert {r.phase for r in plan.rows} == {"dev"}
    assert len({r.case_id for r in plan.rows}) == 6
    assert all(
        r.seed == 926000 + i * 1000 + repeat * 20
        for i in range(6)
        for repeat, r in enumerate(plan.rows[i * 4 : i * 4 + 4])
    )
    assert plan.required_cny <= plan.cap_cny == 3
    assert make_development_plan() == plan

"""Deterministic positive-control queue contracts; no model or container calls."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.positive_control import (
    CANDIDATES,
    FAMILIES,
    InfeasiblePositiveControlError,
    PositiveSelection,
    load_baseline_plan,
    load_plan,
    make_baseline_plan,
    make_baseline_rows,
    make_plan,
    select_tasks,
)


def _results(counts: dict[tuple[str, int], int]) -> dict[str, bool]:
    return {
        row.run_id: row.repeat < counts.get((row.family, row.round or 0), 0)
        for row in make_baseline_rows()
    }


def _feasible_selection() -> tuple[PositiveSelection, ...]:
    return select_tasks(
        make_baseline_rows(),
        _results(
            {
                ("match_rank", 0): 3,
                ("dependency_batches", 0): 4,
                ("table_join", 0): 2,
            }
        ),
    )


def test_plan_shape_and_denominators() -> None:
    plan = make_plan(_feasible_selection(), selection_evidence_sha256="a" * 64)
    assert len(plan.baseline_rows) == 72
    assert len(plan.train_rows) == 72
    assert len(plan.eval_rows) == 72
    assert len({row.group for row in plan.train_rows}) == 18
    assert plan.selected == (
        "curve_v4_path_router_02",
        "curve_v4_dependency_batches_01",
        "curve_v4_table_join_02",
    )
    assert plan.required_cny == Decimal("48.032")
    assert (plan.pass_min, plan.pass_delta, plan.pass_task_min) == (20, 8, 5)
    assert (plan.fail_max, plan.fail_min_updates) == (16, 8)


def test_baseline_queue_excludes_the_reserve_candidate() -> None:
    rows = make_baseline_rows()
    assert {row.case_id for row in rows} == {
        case_id for family in FAMILIES for case_id in CANDIDATES[family][:3]
    }


def test_selection_skips_infeasible_primary_and_uses_fallback() -> None:
    selection = select_tasks(
        make_baseline_rows(),
        _results(
            {
                ("match_rank", 0): 0,
                ("match_rank", 1): 2,
                ("dependency_batches", 0): 5,
                ("dependency_batches", 1): 3,
                ("table_join", 0): 1,
            }
        ),
    )
    assert [(item.family, item.case_id, item.round, item.successes) for item in selection] == [
        ("match_rank", "curve_v4_path_router_01", 1, 2),
        ("dependency_batches", "curve_v4_dependency_batches_03", 1, 3),
        ("table_join", "curve_v4_table_join_02", 0, 1),
    ]


def test_infeasible_family_raises_before_training() -> None:
    with pytest.raises(InfeasiblePositiveControlError):
        select_tasks(
            make_baseline_rows(),
            _results(
                {
                    ("match_rank", 0): 0,
                    ("match_rank", 1): 8,
                    ("match_rank", 2): 5,
                }
            ),
        )


def test_held_out_seeds_are_paired_and_disjoint_from_training() -> None:
    plan = make_plan(_feasible_selection(), selection_evidence_sha256="a" * 64)
    for task_index, case_id in enumerate(plan.selected):
        baseline = {row.seed for row in plan.baseline_rows if row.case_id == case_id}
        evaluation = {row.seed for row in plan.eval_rows if row.case_id == case_id}
        expected = {981_000 + task_index * 100 + repeat for repeat in range(8)}
        assert baseline == expected
        assert evaluation == expected
    assert not ({row.seed for row in plan.train_rows} & {row.seed for row in plan.eval_rows})


def test_plan_rejects_wrong_selection_order() -> None:
    selection = _feasible_selection()
    with pytest.raises(ValueError, match="declared order"):
        make_plan((selection[1], selection[0], selection[2]))


def test_load_plan_rejects_tampering(tmp_path: Path) -> None:
    plan = make_plan(_feasible_selection(), selection_evidence_sha256="a" * 64)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    assert load_plan(path) == plan
    data = json.loads(path.read_text())
    data["train_rows"][0]["seed"] += 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="positive-control plan changed"):
        load_plan(path)


def test_baseline_plan_round_trips_and_rejects_tampering(tmp_path: Path) -> None:
    plan = make_baseline_plan()
    path = tmp_path / "baseline.json"
    path.write_text(plan.model_dump_json())
    assert load_baseline_plan(path) == plan
    data = json.loads(path.read_text())
    data["rows"][0]["seed"] += 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="baseline plan changed"):
        load_baseline_plan(path)

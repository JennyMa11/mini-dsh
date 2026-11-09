"""New training/validation profile excludes all opened dev instances."""

from collections import Counter
from pathlib import Path

import pytest
from minidsh_rl import curve_execution
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.curve_execution import execute_curve
from minidsh_rl.curve_schedule import make_curve_plan
from minidsh_rl.development_cases import public_checker
from minidsh_rl.learning_schedule import atomic_record, digest
from test_curve_execution import ContractRuntime, Resources, group_fixture


def test_v4_split_and_unchanged_private_contract() -> None:
    old = {c.task.case_id: c for c in curve_cases_v2() if c.split != "dev"}
    cases = curve_cases_v4()
    assert len(cases) == 24
    assert Counter(c.split for c in cases) == {"train": 12, "validation": 12}
    assert all(c.variant not in {4, 5} for c in cases)
    for case in cases:
        before = old[case.task.case_id.replace("curve_v4_", "curve_v2_")]
        assert case.faults == before.faults
        assert case.task.reference == before.task.reference
        assert case.task.hidden_checks == before.task.hidden_checks
        assert case.task.specification == before.task.specification
        assert dict(case.task.files)["public_tests.py"] == public_checker(case.task.family)
        assert all(
            text == dict(before.task.files)[name]
            for name, text in case.task.files
            if name != "public_tests.py"
        )


def test_v4_has_192_rows_and_attempted_slot_checkpoints() -> None:
    plan = make_curve_plan(dataset="curve_dataset_v4")
    assert len(plan.rows) == 192
    assert Counter(row.phase for row in plan.rows) == {"train": 96, "validation": 96}
    assert {row.checkpoint for row in plan.rows if row.phase == "validation"} == {0, 8, 16, 24}
    assert all(row.run_id.startswith("curve4_") for row in plan.rows)
    assert plan.required_cny < 60
    assert make_curve_plan(dataset="curve_dataset_v4") == plan


async def test_v4_rejects_missing_readiness_before_initialize(tmp_path: Path) -> None:
    plan = make_curve_plan(dataset="curve_dataset_v4")
    atomic_record(tmp_path / "plan.json", plan, create=True)
    runtime = ContractRuntime()
    with pytest.raises(ValueError, match="external readiness"):
        await execute_curve(
            tmp_path, plan, Resources(), runtime, reader=runtime.reader, builder=group_fixture
        )
    assert runtime.initializations == 0


async def test_v4_contract_curve_reuses_gate_without_new_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = tmp_path / "external-readiness.json"
    proof.write_text('{"kind":"contract_fixture_only"}')
    plan = make_curve_plan(dataset="curve_dataset_v4").model_copy(
        update={"readiness_sha256": digest(proof)}
    )
    atomic_record(tmp_path / "plan.json", plan, create=True)
    monkeypatch.setattr(curve_execution, "verify_readiness", lambda _path: None)
    runtime = ContractRuntime()
    state = await execute_curve(
        tmp_path, plan, Resources(), runtime, reader=runtime.reader, builder=group_fixture
    )
    assert state.phase == "finished"
    assert len(state.completed) == 192
    assert runtime.updates == 24
    assert state.dev_passed
    assert {r for r in state.completed if "_dev_" in r} == set()

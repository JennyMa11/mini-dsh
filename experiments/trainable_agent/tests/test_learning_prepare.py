"""Admission is a local inspection, including when the resource snapshot is sufficient."""

import json
from pathlib import Path

import pytest
from minidsh_rl.learning_execution import ExecutionState
from minidsh_rl.learning_prepare import admission
from minidsh_rl.learning_schedule import atomic_record, digest, prepare


def ledger(path: Path, historical: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "limit": "50",
                "historical": historical,
                "uncertainty": "0",
                "legacy_hash": "a" * 64,
                "quote_hash": "b" * 64,
                "charges": {},
                "bills": {},
            }
        )
    )


@pytest.mark.parametrize(("historical", "capacity"), [("42.667", False), ("0", True)])
def test_inspection_never_opens_execution_or_mutates_budget(
    tmp_path: Path, historical: str, *, capacity: bool
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    budget = tmp_path / "budget.json"
    ledger(budget, historical)
    original = budget.read_bytes()
    report = admission(directory, budget)
    assert report["execution_admitted"] is False
    assert report["financial_capacity"] is capacity
    assert report["declared_rows"] == 80
    assert report["completed_rows"] == 0
    assert report["required_remaining_cny"] == "20.302"
    assert report["new_provider_calls"] == report["new_training_calls"] == 0
    assert budget.read_bytes() == original
    assert not (directory / "state.json").exists()


def test_inspection_refuses_state_that_drops_declared_rows(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    plan = prepare(directory, kind="contract_fixture")
    state = ExecutionState(
        plan_sha256=digest(directory / "plan.json"),
        rows={row.run_id: "not_run" for row in plan.rows[1:]},
    )
    atomic_record(directory / "state.json", state, create=True)
    budget = tmp_path / "budget.json"
    ledger(budget, "0")
    with pytest.raises(ValueError, match="state/plan"):
        admission(directory, budget)


def test_staged_inspection_separates_next_stage_from_full_queue(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    budget = tmp_path / "budget.json"
    ledger(budget, "42.667")
    report = admission(directory, budget)
    assert report["financial_capacity"] is False
    assert report["next_stage_financial_capacity"] is True
    assert report["required_remaining_cny"] == "20.302"
    assert report["next_stage_required_cny"] == "2.15"
    assert report["execution_admitted"] is False
    assert report["declared_rows"] == 80
    assert report["completed_rows"] == 0

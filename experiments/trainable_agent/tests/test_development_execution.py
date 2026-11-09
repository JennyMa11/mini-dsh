"""Deterministic admission fixtures, no provider clients or actual model rewards."""

from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.curve_schedule import CurveRow
from minidsh_rl.development_budget import StudyBudget
from minidsh_rl.development_execution import execute_development
from minidsh_rl.development_schedule import make_development_plan
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_rollout import LearningRollout, PolicyBinding
from minidsh_rl.learning_schedule import atomic_record


class Parent:
    available = Decimal("100")
    committed = Decimal("50")
    path = Path("not_read_in_contract_fixture")

    def reserve(self, operation: str, maximum: Decimal) -> None:
        del operation
        self.committed += maximum

    def settle(self, operation: str, estimate: Decimal) -> None:
        del operation, estimate


POLICY = PolicyBinding(
    kind="scripted_control",
    policy_id="development_contract",
    snapshot_sha256="a" * 64,
    tokenizer_sha256="b" * 64,
)


async def feedback(value: LearningRollout, output: Path) -> bool:
    del output
    return value.success


async def test_admission_complete_once_and_no_training_path(tmp_path: Path) -> None:
    plan = make_development_plan().model_copy(update={"kind": "contract_fixture"})
    atomic_record(tmp_path / "plan.json", plan, create=True)
    calls = []

    async def sample(case: LearningCase, row: CurveRow, directory: Path) -> LearningRollout:
        del directory
        calls.append(row.run_id)
        return LearningRollout(
            case,
            row.run_id,
            row.seed,
            POLICY,
            row.run_id.endswith("_0"),
            "finished",
            (),
            0,
            "a" * 64,
            verification_executed=True,
            local_failed_calls=0,
        )

    budget = StudyBudget(Parent(), Decimal("50"))
    state = await execute_development(tmp_path, plan, budget, POLICY, sample, feedback)
    assert state.phase == "accepted"
    assert len(state.completed) == 24
    assert len(calls) == 24
    again = await execute_development(tmp_path, plan, budget, POLICY, sample, feedback)
    assert again == state
    assert len(calls) == 24
    assert not (tmp_path / "updates").exists()


async def test_insufficient_complete_allocation_never_samples(tmp_path: Path) -> None:
    plan = make_development_plan().model_copy(update={"kind": "contract_fixture"})
    atomic_record(tmp_path / "plan.json", plan, create=True)
    parent = Parent()
    budget = StudyBudget(parent, Decimal("50"))
    parent.committed = Decimal("52")

    async def sample(case: LearningCase, row: CurveRow, directory: Path) -> LearningRollout:
        del case, row, directory
        raise AssertionError("no sample allowed")

    with pytest.raises(ValueError, match="complete"):
        await execute_development(tmp_path, plan, budget, POLICY, sample, feedback)


async def test_interrupted_row_is_not_automatically_resampled(tmp_path: Path) -> None:
    plan = make_development_plan().model_copy(update={"kind": "contract_fixture"})
    atomic_record(tmp_path / "plan.json", plan, create=True)
    calls = []

    async def sample(case: LearningCase, row: CurveRow, directory: Path) -> LearningRollout:
        del case, directory
        calls.append(row.run_id)
        raise RuntimeError("unknown fixture response")

    budget = StudyBudget(Parent(), Decimal("50"))
    with pytest.raises(RuntimeError, match="unknown"):
        await execute_development(tmp_path, plan, budget, POLICY, sample, feedback)
    with pytest.raises(ValueError, match="never replay"):
        await execute_development(tmp_path, plan, budget, POLICY, sample, feedback)
    assert len(calls) == 1


async def test_failed_admission_keeps_the_full_declared_denominator(tmp_path: Path) -> None:
    plan = make_development_plan().model_copy(update={"kind": "contract_fixture"})
    atomic_record(tmp_path / "plan.json", plan, create=True)

    async def sample(case: LearningCase, row: CurveRow, directory: Path) -> LearningRollout:
        del directory
        return LearningRollout(
            case=case,
            run_id=row.run_id,
            seed=row.seed,
            policy=POLICY,
            success=False,
            status="failed",
            turns=(),
            total_tokens=0,
            harness_sha256="a" * 64,
            verification_executed=True,
            local_failed_calls=0,
        )

    state = await execute_development(
        tmp_path, plan, StudyBudget(Parent(), Decimal("50")), POLICY, sample, feedback
    )
    assert state.phase == "rejected"
    assert len(state.completed) == 24
    assert len(state.rewards) == 6

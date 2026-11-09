"""Explicit contract fixtures test scheduling/recovery; never provider evidence."""

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl.contracts import Turn, advantages
from minidsh_rl.curve_execution import CurveState, execute_curve
from minidsh_rl.curve_schedule import CurveRow, make_curve_plan
from minidsh_rl.datum import build_batch
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import Checkpoint, group_datum_hash, write_update_receipt
from minidsh_rl.learning_rollout import LearningGroup, LearningRollout, PolicyBinding
from minidsh_rl.learning_schedule import atomic_record
from minidsh_rl.transaction import UpdateJournal


class Resources:
    available = Decimal("100")
    committed = Decimal("0")
    has_pending = False


def group_fixture(values: tuple[LearningRollout, ...]) -> LearningGroup:
    rewards = tuple(float(v.success) for v in values)
    relative = advantages(rewards)
    return LearningGroup(
        tuple(v.run_id for v in values),
        rewards,
        relative,
        build_batch(tuple(v.turns for v in values), relative),
    )


class ContractRuntime:
    execution_kind: Literal["provider", "contract_fixture"] = "contract_fixture"

    def __init__(self, *, success: bool = True, fail_at: int = -1) -> None:
        self.values: dict[str, LearningRollout] = {}
        self.success, self.fail_at = success, fail_at
        self.updates = 0
        self.initializations = 0
        self.restores = 0

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(
            execution_kind="contract_fixture",
            sampler_uri=f"contract://sampler/{self.updates}",
            optimizer_uri=f"contract://optimizer/{self.updates}",
            policy=PolicyBinding(
                kind="scripted_control",
                policy_id=f"control_{self.updates}",
                snapshot_sha256="a" * 64,
                tokenizer_sha256="b" * 64,
            ),
        )

    async def initialize(self, directory: Path) -> Checkpoint:
        self.initializations += 1
        value = self.checkpoint()
        atomic_record(directory / "initial.json", value, create=True)
        return value

    async def reconnect(self, checkpoint: Checkpoint, attempt: int) -> None:
        assert checkpoint == self.checkpoint()
        assert 1 <= attempt <= 2
        self.restores += 1

    async def sample(
        self, case: LearningCase, row: CurveRow, before: Checkpoint, output: Path
    ) -> LearningRollout:
        output.mkdir(parents=True)  # noqa: ASYNC240 - tiny local contract fixture only
        (output / "fixture.json").write_text('{"kind":"contract_fixture"}')
        value = LearningRollout(
            case,
            row.run_id,
            row.seed,
            before.policy,
            self.success and row.run_id.endswith(("_0", "_1")),
            "finished",
            (Turn((1,), (2,), (-0.2,)),),
            2,
            "a" * 64,
            verification_executed=True,
            local_failed_calls=0,
        )
        self.values[row.run_id] = value
        if len(self.values) == self.fail_at:
            raise RuntimeError("confirmed fixture response before controller save")
        return value

    def reader(self, case: LearningCase, folder: Path, policy: PolicyBinding) -> LearningRollout:
        value = self.values[folder.name]
        assert value.case == case
        assert value.policy == policy
        return value

    async def feedback(self, value: LearningRollout, output: Path) -> bool:
        del output
        return value.success

    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, output: Path
    ) -> None:
        journal = UpdateJournal.create(
            output / f"update_{int(key.rsplit('_', 1)[-1]):03}.json", group_datum_hash(group)
        )
        updated = group.batch is not None and group.batch.has_signal
        if updated:
            for phase in (
                "backward_submitted",
                "backward_confirmed",
                "optim_submitted",
                "optim_confirmed",
            ):
                journal.advance(phase)
            self.updates += 1
            after = self.checkpoint()
            journal.advance("checkpointed", checkpoint=after.optimizer_uri)
        else:
            journal.advance("skipped")
            after = before
        write_update_receipt(output, key, group, before, after, updated=updated)


def prepare(root: Path) -> None:
    atomic_record(root / "plan.json", make_curve_plan(), create=True)


async def test_all_four_checkpoints_and_no_duplicate_completed_execution(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ContractRuntime()
    plan = make_curve_plan()
    state = await execute_curve(
        tmp_path, plan, Resources(), runtime, reader=runtime.reader, builder=group_fixture
    )
    assert state.phase == "finished"
    assert len(state.completed) == 216
    assert runtime.updates == 24
    assert set(state.snapshots) == {0, 8, 16, 24}
    again = await execute_curve(
        tmp_path, plan, Resources(), runtime, reader=runtime.reader, builder=group_fixture
    )
    assert again == state
    assert runtime.updates == 24


async def test_dev_failure_stops_all_training(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ContractRuntime(success=False)
    state = await execute_curve(
        tmp_path,
        make_curve_plan(),
        Resources(),
        runtime,
        reader=runtime.reader,
        builder=group_fixture,
    )
    assert state.phase == "dev_failed"
    assert len(state.completed) == 24
    assert runtime.updates == 0
    assert not state.snapshots


async def test_confirmed_rollout_recovery_does_not_resample(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ContractRuntime(fail_at=5)
    with pytest.raises(RuntimeError, match="confirmed"):
        await execute_curve(
            tmp_path,
            make_curve_plan(),
            Resources(),
            runtime,
            reader=runtime.reader,
            builder=group_fixture,
        )
    state = await execute_curve(
        tmp_path,
        make_curve_plan(),
        Resources(),
        runtime,
        resume=True,
        reader=runtime.reader,
        builder=group_fixture,
    )
    assert state.phase == "finished"
    assert runtime.initializations == runtime.restores == 1
    assert len(runtime.values) == 216


async def test_unknown_resume_rejected_before_runtime(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ContractRuntime()
    state = CurveState(
        plan_sha256=__import__("hashlib").sha256((tmp_path / "plan.json").read_bytes()).hexdigest(),
        kind="contract_fixture",
        pending="resume",
    )
    atomic_record(tmp_path / "curve-state.json", state)
    with pytest.raises(ValueError, match="never replay"):
        await execute_curve(
            tmp_path,
            make_curve_plan(),
            Resources(),
            runtime,
            resume=True,
            reader=runtime.reader,
            builder=group_fixture,
        )
    assert runtime.initializations == 0


class ZeroTrainingRuntime(ContractRuntime):
    async def sample(
        self, case: LearningCase, row: CurveRow, before: Checkpoint, output: Path
    ) -> LearningRollout:
        value = await super().sample(case, row, before, output)
        if row.phase == "train":
            value = __import__("dataclasses").replace(value, success=False)
            self.values[row.run_id] = value
        return value


async def test_zero_signal_groups_are_slots_not_resampled(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ZeroTrainingRuntime()
    state = await execute_curve(
        tmp_path,
        make_curve_plan(),
        Resources(),
        runtime,
        reader=runtime.reader,
        builder=group_fixture,
    )
    assert state.phase == "zero_signal_stop"
    assert state.zero_streak == 10
    assert len(state.groups) == 10
    assert runtime.updates == 0
    assert set(state.snapshots) == {0, 8}


class ConfirmedUpdateFailure(ContractRuntime):
    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, output: Path
    ) -> None:
        await super().update(key, group, before, output)
        if self.updates == 1:
            raise RuntimeError("receipt saved before controller confirmation")


async def test_confirmed_update_recovers_without_duplicate_optimizer(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = ConfirmedUpdateFailure()
    with pytest.raises(RuntimeError, match="receipt saved"):
        await execute_curve(
            tmp_path,
            make_curve_plan(),
            Resources(),
            runtime,
            reader=runtime.reader,
            builder=group_fixture,
        )
    state = await execute_curve(
        tmp_path,
        make_curve_plan(),
        Resources(),
        runtime,
        resume=True,
        reader=runtime.reader,
        builder=group_fixture,
    )
    assert state.phase == "finished"
    assert runtime.updates == 24


async def test_unknown_update_refuses_replay(tmp_path: Path) -> None:
    prepare(tmp_path)
    plan_path = tmp_path / "plan.json"
    state = CurveState(
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        kind="contract_fixture",
        pending="update",
        key="train_001",
    )
    atomic_record(tmp_path / "curve-state.json", state)
    runtime = ContractRuntime()
    with pytest.raises(FileNotFoundError):
        await execute_curve(
            tmp_path,
            make_curve_plan(),
            Resources(),
            runtime,
            resume=True,
            reader=runtime.reader,
            builder=group_fixture,
        )
    assert runtime.initializations == runtime.updates == 0

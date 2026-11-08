"""Contract fixtures exercise control flow, never empirical policy or SDK outcomes."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl import learning_execution
from minidsh_rl.contracts import Turn
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import (
    Checkpoint,
    ExecutionState,
    execute,
    group_datum_hash,
    write_update_receipt,
)
from minidsh_rl.learning_rollout import LearningGroup, LearningRollout, PolicyBinding
from minidsh_rl.learning_schedule import FundingPolicy, QueueRow, atomic_record, prepare
from minidsh_rl.transaction import UpdateJournal

from minidsh.infrastructure.locks import FileLease


@dataclass
class Resources:
    available: Decimal = Decimal("30")
    committed: Decimal = Decimal("0")
    has_pending: bool = False


def checkpoint(index: int) -> Checkpoint:
    return Checkpoint(
        execution_kind="contract_fixture",
        sampler_uri=f"contract://sampler/{index}",
        optimizer_uri=f"contract://optimizer/{index}",
        policy=PolicyBinding(
            kind="provider",
            policy_id=f"unit_{index}",
            snapshot_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
            tokenizer_sha256="b" * 64,
        ),
    )


def read(case: LearningCase, directory: Path, policy: PolicyBinding) -> LearningRollout:
    raw = json.loads((directory / "fixture.json").read_text())
    if raw["policy"] != policy.policy_id:
        raise ValueError("fixture policy mismatch")
    return LearningRollout(
        case,
        raw["run_id"],
        raw["seed"],
        policy,
        raw["success"],
        "finished",
        (Turn((1,), (2,), (-0.1,)),),
        2,
        "c" * 64,
        verification_executed=True,
        local_failed_calls=0,
    )


class Runtime:
    execution_kind: Literal["contract_fixture"] = "contract_fixture"

    def __init__(
        self,
        *,
        unknown: bool = False,
        after_receipt: bool = False,
        readiness: bool = True,
        zero_signal: bool = False,
    ):
        self.opens = 0
        self.restores = 0
        self.updates = 0
        self.optim_steps = 0
        self.runs: list[str] = []
        self.feedback_calls = 0
        self.unknown = unknown
        self.after_receipt = after_receipt
        self.readiness = readiness
        self.zero_signal = zero_signal

    async def initialize(self, directory: Path) -> Checkpoint:
        self.opens += 1
        value = checkpoint(0)
        atomic_record(directory / "initial.json", value, create=True)
        return value

    async def restore(self, before: Checkpoint, directory: Path) -> None:
        del before, directory
        self.restores += 1

    async def rollout(
        self, case: LearningCase, row: QueueRow, before: Checkpoint, directory: Path
    ) -> LearningRollout:
        await asyncio.to_thread(directory.mkdir, parents=True)
        self.runs.append(row.run_id)
        success = (row.seed % 100) % 2 == 0 if self.readiness else False
        if self.zero_signal and row.phase == "train":
            success = False
        (directory / "fixture.json").write_text(
            json.dumps(
                {
                    "policy": before.policy.policy_id,
                    "run_id": row.run_id,
                    "seed": row.seed,
                    "success": success,
                    "kind": "contract_fixture",
                }
            )
        )
        return read(case, directory, before.policy)

    async def feedback(self, rollout: LearningRollout, directory: Path) -> bool:
        del directory
        self.feedback_calls += 1
        return rollout.success and rollout.case.family_group in {"dependency_batches", "table_join"}

    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, directory: Path
    ) -> None:
        self.updates += 1
        slot = int(key.rsplit("_", maxsplit=1)[-1])
        await asyncio.to_thread(directory.mkdir, exist_ok=True)
        journal = UpdateJournal.create(
            directory / f"update_{slot:03}.json", group_datum_hash(group)
        )
        if group.batch is None or not group.batch.has_signal:
            journal.advance("skipped")
            write_update_receipt(directory, key, group, before, before, updated=False)
            return
        journal.advance("backward_submitted")
        journal.advance("backward_confirmed")
        journal.advance("optim_submitted")
        self.optim_steps += 1
        if self.unknown:
            raise RuntimeError("unknown optimizer result")
        journal.advance("optim_confirmed")
        after = checkpoint(slot + 1)
        journal.advance("checkpointed", checkpoint=after.optimizer_uri)
        write_update_receipt(directory, key, group, before, after, updated=True)
        if self.after_receipt:
            self.after_receipt = False
            raise RuntimeError("crash after confirmed receipt")


async def test_full_control_preserves_all_denominators_and_does_not_reinitialize(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    runtime = Runtime()
    state = await execute(directory, Resources(), lambda: runtime, reader=read)
    assert state.phase == "finished"
    assert len(state.rows) == 80
    assert all(value == "completed" for value in state.rows.values())
    assert runtime.opens == 1
    assert runtime.updates == 8
    assert len(runtime.runs) == 80
    assert state.current == checkpoint(8)
    await execute(directory, Resources(), lambda: runtime, reader=read)
    assert runtime.opens == 1
    assert len(runtime.runs) == 80
    with (directory / "updates/train_000/update_000.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="artifact"):
        await execute(directory, Resources(), lambda: runtime, reader=read)


async def test_insufficient_full_budget_and_pending_costs_fail_before_factory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    runtime = Runtime()
    factories = []

    def factory() -> Runtime:
        factories.append(True)
        return runtime

    for budget in (Resources(available=Decimal("7.333")), Resources(has_pending=True)):
        with pytest.raises(ValueError, match=r"budget|pending"):
            await execute(directory, budget, factory, reader=read)
    assert runtime.opens == runtime.restores == 0
    assert runtime.runs == []
    assert factories == []


async def test_unknown_optimizer_is_never_replayed_on_resume(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    runtime = Runtime(unknown=True)
    with pytest.raises(RuntimeError, match="unknown"):
        await execute(directory, Resources(), lambda: runtime, reader=read)
    replacement = Runtime()
    with pytest.raises(ValueError, match="unknown"):
        await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    assert replacement.opens == replacement.restores == replacement.updates == 0
    assert runtime.updates == 1


@pytest.mark.parametrize("funding_policy", ["full_remaining_v1", "confirmed_stage_v1"])
async def test_confirmed_receipt_recovers_without_replaying_gradient(
    tmp_path: Path,
    funding_policy: FundingPolicy,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy=funding_policy)
    runtime = Runtime(after_receipt=True)
    with pytest.raises(RuntimeError, match="receipt"):
        await execute(directory, Resources(), lambda: runtime, reader=read)
    replacement = Runtime()
    state = await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    assert state.phase == "finished"
    assert replacement.restores == 1
    assert replacement.opens == 0
    assert replacement.feedback_calls == 0
    assert runtime.updates + replacement.updates == 8
    assert len(runtime.runs) + len(replacement.runs) == 80


@pytest.mark.parametrize("funding_policy", ["full_remaining_v1", "confirmed_stage_v1"])
async def test_failed_readiness_retains_all_unrun_training_and_test_rows(
    tmp_path: Path,
    funding_policy: FundingPolicy,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy=funding_policy)
    runtime = Runtime(readiness=False)
    state = await execute(directory, Resources(), lambda: runtime, reader=read)
    assert state.phase == "readiness_failed"
    assert len(runtime.runs) == 16
    assert len(state.rows) == 80
    assert sum(value == "not_run" for value in state.rows.values()) == 64
    assert runtime.updates == 0


@pytest.mark.parametrize("funding_policy", ["full_remaining_v1", "confirmed_stage_v1"])
async def test_zero_signal_groups_keep_rows_and_checkpoint_without_optimizer(
    tmp_path: Path,
    funding_policy: FundingPolicy,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy=funding_policy)
    runtime = Runtime(zero_signal=True)
    state = await execute(directory, Resources(), lambda: runtime, reader=read)
    assert state.phase == "finished"
    assert len(runtime.runs) == 80
    assert len(state.committed_groups) == 8
    assert state.current == state.initial == checkpoint(0)
    assert runtime.optim_steps == 0
    for slot in range(8):
        journal = UpdateJournal.load(directory / f"updates/train_{slot:03}/update_{slot:03}.json")
        assert journal.state == "skipped"
        assert journal.safe_to_resume


async def test_prepared_group_can_resume_before_any_update_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    runtime = Runtime()
    original = learning_execution._save

    def fail_before_pending(
        root: Path, state: learning_execution.ExecutionState, name: str, key: str = ""
    ) -> None:
        if name == "update_started":
            raise RuntimeError("before pending save")
        original(root, state, name, key)

    monkeypatch.setattr(learning_execution, "_save", fail_before_pending)
    with pytest.raises(RuntimeError, match="before pending"):
        await execute(directory, Resources(), lambda: runtime, reader=read)
    assert runtime.updates == 0
    monkeypatch.setattr(learning_execution, "_save", original)
    replacement = Runtime()
    state = await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    assert state.phase == "finished"
    assert replacement.updates == 8
    assert len(runtime.runs) + len(replacement.runs) == 80


async def test_completed_artifact_change_and_second_writer_block_resume(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    runtime = Runtime(after_receipt=True)
    with pytest.raises(RuntimeError, match="receipt"):
        await execute(directory, Resources(), lambda: runtime, reader=read)
    replacement = Runtime()
    lease = FileLease(directory / "controller.lock")
    try:
        with pytest.raises(ValueError, match="busy"):
            await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    finally:
        lease.close()
    with next((directory / "rollouts").glob("*/fixture.json")).open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="artifact"):
        await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    assert replacement.opens == replacement.restores == replacement.updates == 0


@pytest.mark.parametrize("funding_policy", ["full_remaining_v1", "confirmed_stage_v1"])
async def test_initial_preparation_cost_is_counted_once_after_receipt_crash(
    tmp_path: Path,
    funding_policy: FundingPolicy,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy=funding_policy)

    class PreparingRuntime(Runtime):
        async def initialize(self, directory: Path) -> Checkpoint:
            initial = Checkpoint.model_validate(
                checkpoint(0).model_dump() | {"preparation_spent": "0.02"}
            )
            atomic_record(directory / "initial.json", initial, create=True)
            raise RuntimeError("after initial receipt")

    with pytest.raises(RuntimeError, match="initial receipt"):
        await execute(directory, Resources(), PreparingRuntime, reader=read)
    state = await execute(directory, Resources(), Runtime, reader=read, resume=True)
    assert state.phase == "finished"
    assert state.extra_spent == Decimal("0.03")
    assert state.initial_artifacts


@pytest.mark.parametrize("field", ["resumes", "extra_spent"])
def test_negative_resource_state_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        ExecutionState.model_validate({"plan_sha256": "a" * 64, "rows": {}, field: -1})


async def test_pre_factory_hook_is_after_budget_gate_and_before_factory(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture")
    order = []

    async def preflight() -> None:
        order.append("preflight")

    def factory() -> Runtime:
        order.append("factory")
        return Runtime()

    with pytest.raises(ValueError, match="budget"):
        await execute(
            directory,
            Resources(available=Decimal("1")),
            factory,
            reader=read,
            before_factory=preflight,
        )
    assert order == []
    await execute(directory, Resources(), factory, reader=read, before_factory=preflight)
    assert order == ["preflight", "factory"]


class PricedRuntime(Runtime):
    """Symbolic contract charges, never provider billing or empirical trajectories."""

    def __init__(self, resources: Resources, **kwargs: bool) -> None:
        super().__init__(**kwargs)
        self.resources = resources

    def charge(self, amount: str) -> None:
        self.resources.available -= Decimal(amount)
        self.resources.committed += Decimal(amount)

    async def initialize(self, directory: Path) -> Checkpoint:
        value = await super().initialize(directory)
        self.charge(".21")
        return value

    async def restore(self, before: Checkpoint, directory: Path) -> None:
        await super().restore(before, directory)
        self.charge(".01")

    async def rollout(
        self, case: LearningCase, row: QueueRow, before: Checkpoint, directory: Path
    ) -> LearningRollout:
        value = await super().rollout(case, row, before, directory)
        self.charge(".09")
        return value

    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, directory: Path
    ) -> None:
        await super().update(key, group, before, directory)
        self.charge("1.549")


async def test_staged_readiness_is_funded_whole_and_waits_before_training(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    resources = Resources(available=Decimal("2.15"), committed=Decimal("47.85"))
    runtime = PricedRuntime(resources)
    state = await execute(directory, resources, lambda: runtime, reader=read)
    assert state.phase == "awaiting_settlement"
    assert len(runtime.runs) == 16
    assert runtime.updates == 0
    assert state.readiness_sha256 is not None
    assert state.pending_kind is None
    assert state.resumes == 0
    assert sum(v == "not_run" for v in state.rows.values()) == 64
    before = (directory / "state.json").read_bytes()
    factories = []

    def forbidden() -> Runtime:
        factories.append(True)
        return Runtime()

    for _ in range(3):
        again = await execute(directory, resources, forbidden, reader=read, resume=True)
        assert again == state
    assert factories == []
    assert (directory / "state.json").read_bytes() == before


async def test_staged_shortfall_and_pending_do_not_open_preflight_or_factory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    calls = []

    async def hook() -> None:
        calls.append("preflight")

    def factory() -> Runtime:
        calls.append("factory")
        return Runtime()

    state = await execute(
        directory,
        Resources(available=Decimal("2.149")),
        factory,
        reader=read,
        before_factory=hook,
    )
    assert state.phase == "awaiting_settlement"
    assert state.initial is None
    assert state.resumes == 0
    with pytest.raises(ValueError, match="pending"):
        await execute(
            directory,
            Resources(has_pending=True),
            factory,
            reader=read,
            resume=True,
            before_factory=hook,
        )
    assert calls == []


async def test_staged_complete_sequence_resumes_without_replaying_rows_or_updates(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "study"
    plan = prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    resources = Resources(available=Decimal("4"), committed=Decimal("46"))
    runtimes = []

    def factory() -> PricedRuntime:
        value = PricedRuntime(resources)
        runtimes.append(value)
        return value

    state = await execute(directory, resources, factory, reader=read)
    settlements = 0
    while state.phase == "awaiting_settlement":
        assert settlements < 10
        assert state.pending_kind is None
        settlements += 1
        # Hypothetical confirmed lower bill, not proof of real provider discounts.
        resources.committed = Decimal("46") + Decimal(".05") * settlements
        resources.available = Decimal("50") - resources.committed
        state = await execute(directory, resources, factory, reader=read, resume=True)
    assert state.phase == "finished"
    assert settlements == 9
    assert len(state.rows) == 80
    assert all(v == "completed" for v in state.rows.values())
    assert [r for runtime in runtimes for r in runtime.runs] == [r.run_id for r in plan.rows]
    assert sum(runtime.opens for runtime in runtimes) == 1
    assert sum(runtime.restores for runtime in runtimes) == state.resumes == 9
    assert sum(runtime.optim_steps for runtime in runtimes) == 8
    assert state.extra_spent == Decimal(".09")
    assert state.current == checkpoint(8)
    assert resources.committed < Decimal("50")


async def test_staged_unknown_optimizer_remains_unrecoverable(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    runtime = Runtime(unknown=True)
    with pytest.raises(RuntimeError, match="unknown"):
        await execute(directory, Resources(), lambda: runtime, reader=read)
    replacement = Runtime()
    with pytest.raises(ValueError, match="unknown"):
        await execute(directory, Resources(), lambda: replacement, reader=read, resume=True)
    assert replacement.opens == replacement.restores == replacement.updates == 0


async def test_staged_exhausted_resumes_refuse_before_preflight_or_factory(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    resources = Resources(available=Decimal("2.15"), committed=Decimal("47.85"))
    state = await execute(directory, resources, lambda: PricedRuntime(resources), reader=read)
    state.resumes = 12
    state.extra_spent = Decimal(".12")
    atomic_record(directory / "state.json", state)
    resources.committed, resources.available = Decimal("47.9"), Decimal("2.1")
    calls = []

    async def hook() -> None:
        calls.append("preflight")

    def factory() -> Runtime:
        calls.append("factory")
        return Runtime()

    with pytest.raises(ValueError, match="resume"):
        await execute(directory, resources, factory, reader=read, resume=True, before_factory=hook)
    assert calls == []


async def test_staged_preflight_capacity_change_refuses_before_factory(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    resources = Resources()
    calls = []

    async def hook() -> None:
        calls.append("preflight")
        resources.available = Decimal("1")

    def factory() -> Runtime:
        calls.append("factory")
        return Runtime()

    state = await execute(directory, resources, factory, reader=read, before_factory=hook)
    assert state.phase == "awaiting_settlement"
    assert calls == ["preflight"]
    assert all(value == "not_run" for value in state.rows.values())


async def test_known_cumulative_overrun_never_becomes_funding_wait(tmp_path: Path) -> None:
    directory = tmp_path / "study"
    prepare(directory, kind="contract_fixture", funding_policy="confirmed_stage_v1")
    calls = []

    def factory() -> Runtime:
        calls.append(True)
        return Runtime()

    with pytest.raises(ValueError, match="cumulative"):
        await execute(
            directory,
            Resources(available=Decimal("-1"), committed=Decimal("51")),
            factory,
            reader=read,
        )
    assert calls == []

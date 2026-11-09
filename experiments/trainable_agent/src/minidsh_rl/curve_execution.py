"""Leased single-seed coordinator with explicit known-receipt recovery only."""

import json
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.curve_artifacts import tree_hashes, verify_hashes
from minidsh_rl.curve_readiness import verify_readiness
from minidsh_rl.curve_schedule import (
    TRAIN_SLOTS,
    CurvePlan,
    CurveRow,
    curve_cases_for,
    readiness_gate,
)
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import (
    Checkpoint,
    GroupRecord,
    ResourcePort,
    UpdateReceipt,
    group_datum_hash,
)
from minidsh_rl.learning_gates import _sources
from minidsh_rl.learning_rollout import (
    LearningGroup,
    LearningRollout,
    PolicyBinding,
    assemble_group,
    load_rollout,
)
from minidsh_rl.learning_schedule import (
    GROUP_CLOSE_COST,
    GROUP_TRAIN_COST,
    RUN_COST,
    atomic_record,
    digest,
)
from minidsh_rl.transaction import UpdateJournal

MAX_RESUMES = 2
ZERO_SIGNAL_LIMIT = 10


class CurveRuntime(Protocol):
    @property
    def execution_kind(self) -> Literal["provider", "contract_fixture"]: ...

    async def initialize(self, directory: Path) -> Checkpoint: ...
    async def reconnect(self, checkpoint: Checkpoint, attempt: int) -> None: ...
    async def sample(
        self, case: LearningCase, row: CurveRow, before: Checkpoint, output: Path
    ) -> LearningRollout: ...
    async def feedback(self, value: LearningRollout, output: Path) -> bool: ...
    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, output: Path
    ) -> None: ...


class CurveState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    plan_sha256: str
    phase: Literal["running", "dev_failed", "zero_signal_stop", "finished", "stopped"] = "running"
    kind: Literal["provider", "contract_fixture"]
    initial: Checkpoint | None = None
    current: Checkpoint | None = None
    pending: Literal["initialize", "sample", "update", "resume"] | None = None
    key: str = ""
    completed: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    snapshots: dict[int, Checkpoint] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    resumes: int = 0
    zero_streak: int = 0
    dev_passed: bool = False


def remaining_allocation(plan: CurvePlan, state: CurveState) -> Decimal:
    rows = sum(row.run_id not in state.completed for row in plan.rows)
    groups = TRAIN_SLOTS - len(state.groups)
    return rows * RUN_COST + groups * (GROUP_TRAIN_COST + GROUP_CLOSE_COST) + Decimal("2.25")


def accept_update(root: Path, state: CurveState, key: str) -> None:
    folder = root / "updates" / key
    group = GroupRecord.model_validate_json((folder / "group.json").read_bytes())
    receipt = UpdateReceipt.model_validate_json((folder / "receipt.json").read_bytes())
    verify_hashes(root, group.input_sha256)
    journal_path = folder / f"update_{int(key.rsplit('_', 1)[-1]):03}.json"
    journal = UpdateJournal.load(journal_path)
    if (
        group.before != state.current
        or receipt.before != group.before
        or receipt.group_sha256 != digest(folder / "group.json")
        or receipt.journal_sha256 != digest(journal_path)
        or journal.datum_hash != group.datum_sha256
        or not journal.safe_to_resume
        or receipt.updated != group.has_signal
        or (receipt.updated and journal.checkpoint != receipt.after.optimizer_uri)
        or (not receipt.updated and receipt.after != receipt.before)
    ):
        raise ValueError("unconfirmed/changed update; never replay")
    if receipt.after.execution_kind != state.kind:
        raise ValueError("checkpoint provenance changed")
    state.current = receipt.after
    state.groups.append(key)
    if receipt.updated:
        state.updated.append(key)
        state.zero_streak = 0
    else:
        state.zero_streak += 1
    state.artifacts.update(tree_hashes(root, folder))
    state.artifacts.update(tree_hashes(root, root / "policies"))
    state.pending, state.key = None, ""


async def execute_curve(  # noqa: PLR0913, PLR0912, PLR0915 - explicit leased state machine
    root: Path,
    plan: CurvePlan,
    budget: ResourcePort,
    runtime: CurveRuntime,
    *,
    resume: bool = False,
    reader: Callable[[LearningCase, Path, PolicyBinding], LearningRollout] = load_rollout,
    builder: Callable[[tuple[LearningRollout, ...]], LearningGroup] = assemble_group,
) -> CurveState:
    """Injected contract runtimes cannot be passed to a real provider CLI."""
    lease = FileLease(root / "controller.lock")
    try:
        plan_path = root / "plan.json"
        if CurvePlan.model_validate_json(plan_path.read_bytes()) != plan:
            raise ValueError("frozen plan changed")
        state_path = root / "curve-state.json"
        state = (
            CurveState.model_validate_json(state_path.read_bytes())
            if state_path.exists()
            else CurveState(plan_sha256=digest(plan_path), kind=runtime.execution_kind)
        )
        if state.plan_sha256 != digest(plan_path) or state.kind != runtime.execution_kind:
            raise ValueError("controller identity changed")
        if runtime.execution_kind == "provider" and builder is not assemble_group:
            raise ValueError("provider requires the real group builder")
        verify_hashes(root, state.artifacts)
        if state.phase != "running":
            return state
        if budget.has_pending:
            raise ValueError("unknown financial operation; do not replay")
        cases = {c.task.case_id: c for c in curve_cases_for(plan.dataset_version)}
        if plan.dataset_version == "curve_dataset_v4":
            proof = root / "external-readiness.json"
            if plan.readiness_sha256 is None or digest(proof) != plan.readiness_sha256:
                raise ValueError("frozen external readiness required")
            verify_readiness(proof)
            state.dev_passed = True
            state.artifacts["external-readiness.json"] = plan.readiness_sha256

        def invariant() -> None:
            if digest(plan_path) != state.plan_sha256:
                raise ValueError("plan changed during execution")
            if state.kind == "provider" and _sources() != plan.sources:
                raise ValueError("source changed during execution")

        def save() -> None:
            atomic_record(state_path, state)

        def check_row(row: CurveRow, value: LearningRollout, before: Checkpoint) -> None:
            if (
                value.case != cases[row.case_id]
                or value.run_id != row.run_id
                or value.seed != row.seed
                or value.policy != before.policy
                or value.budget_violations
                or value.status == "cancelled"
            ):
                raise ValueError("trajectory identity/budget/cancellation failure")

        def complete(row: CurveRow, value: LearningRollout, before: Checkpoint) -> None:
            check_row(row, value, before)
            state.completed.append(row.run_id)
            state.artifacts.update(tree_hashes(root, root / "rollouts" / row.run_id))
            state.pending, state.key = None, ""
            save()

        if state.pending:
            if not resume:
                raise ValueError("explicit safe resume required")
            if state.pending == "update":
                accept_update(root, state, state.key)
            elif state.pending == "sample" and state.current:
                row = next(row for row in plan.rows if row.run_id == state.key)
                before = state.initial if row.phase == "dev" else state.current
                if before is None:
                    raise ValueError("initial checkpoint missing")
                complete(
                    row,
                    reader(cases[row.case_id], root / "rollouts" / row.run_id, before.policy),
                    before,
                )
            elif state.pending == "initialize" and (root / "initial.json").exists():
                state.current = state.initial = Checkpoint.model_validate_json(
                    (root / "initial.json").read_bytes()
                )
                state.pending = None
                state.artifacts.update(tree_hashes(root, root / "initial-sdk"))
                state.artifacts.update(tree_hashes(root, root / "policies"))
            else:
                raise ValueError("unknown operation; never replay")
            save()
        if budget.available < remaining_allocation(plan, state):
            raise ValueError("complete remaining curve does not fit budget")
        if state.initial is None:
            state.pending = "initialize"
            save()
            checkpoint = await runtime.initialize(root)
            if checkpoint.execution_kind != state.kind:
                raise ValueError("initial provenance mismatch")
            state.initial = state.current = checkpoint
            state.pending = None
            state.artifacts.update(tree_hashes(root, root / "initial-sdk"))
            state.artifacts.update(tree_hashes(root, root / "policies"))
            if (root / "initial.json").exists():
                state.artifacts["initial.json"] = digest(root / "initial.json")
            save()
        else:
            if not resume or state.resumes >= MAX_RESUMES or state.current is None:
                raise ValueError("explicit bounded resume required")
            state.resumes += 1
            state.pending = "resume"
            save()
            await runtime.reconnect(state.current, state.resumes)
            state.pending = None
            save()

        async def sample(row: CurveRow) -> LearningRollout:
            invariant()
            before = state.initial if row.phase == "dev" else state.current
            if before is None:
                raise ValueError("checkpoint missing")
            folder = root / "rollouts" / row.run_id
            if row.run_id in state.completed:
                value = reader(cases[row.case_id], folder, before.policy)
                check_row(row, value, before)
                return value
            if budget.has_pending or budget.available < remaining_allocation(plan, state):
                raise ValueError("complete queue or financial status no longer admitted")
            state.pending, state.key = "sample", row.run_id
            save()
            value = await runtime.sample(cases[row.case_id], row, before, folder)
            complete(row, value, before)
            sys.stdout.write(
                json.dumps(
                    {
                        "phase": row.phase,
                        "completed": len(state.completed),
                        "declared": len(plan.rows),
                        "success": value.success,
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            return value

        if not state.dev_passed:
            rewards: dict[str, list[bool]] = {}
            families: dict[str, str] = {}
            feedback = set()
            for row in (r for r in plan.rows if r.phase == "dev"):
                value = await sample(row)
                rewards.setdefault(row.group, []).append(value.success)
                families[row.group] = value.case.family_group
                if value.success and await runtime.feedback(value, root / "feedback" / row.run_id):
                    feedback.add(value.case.family_group)
            allowed = readiness_gate(list(rewards.values()), list(families.values()), feedback)
            report = {
                "rewards": rewards,
                "families": families,
                "feedback_groups": sorted(feedback),
                "passed": allowed,
                "completed": 24,
                "declared": 24,
            }
            path = root / "readiness.json"
            if path.exists() and json.loads(path.read_text()) != report:
                raise ValueError("readiness evidence changed")
            path.write_text(json.dumps(report, indent=2))
            state.artifacts["readiness.json"] = digest(path)
            state.artifacts.update(tree_hashes(root, root / "feedback"))
            state.dev_passed = allowed
            if not allowed:
                state.phase = "dev_failed"
                save()
                return state
            save()
        for slot in range(TRAIN_SLOTS + 1):
            key = f"train_{slot:03}"
            if slot and key not in state.groups:
                rows = [r for r in plan.rows if r.phase == "train" and r.group == key]
                values = tuple([await sample(row) for row in rows])
                group = builder(values)
                if state.current is None:
                    raise ValueError("current policy missing")
                folder = root / "updates" / key
                folder.mkdir(parents=True, exist_ok=True)
                record = GroupRecord(
                    before=state.current,
                    run_ids=group.run_ids,
                    datum_sha256=group_datum_hash(group),
                    has_signal=group.batch is not None and group.batch.has_signal,
                    input_sha256={
                        p: h for p, h in state.artifacts.items() if any(r.run_id in p for r in rows)
                    },
                )
                atomic_record(folder / "group.json", record, create=True)
                state.pending, state.key = "update", key
                save()
                invariant()
                await runtime.update(key, group, state.current, folder)
                accept_update(root, state, key)
                save()
                if state.zero_streak >= ZERO_SIGNAL_LIMIT:
                    state.phase = "zero_signal_stop"
                    save()
                    return state
            validation = [r for r in plan.rows if r.phase == "validation" and r.checkpoint == slot]
            if validation:
                if slot not in state.snapshots:
                    if state.current is None:
                        raise ValueError("validation policy missing")
                    state.snapshots[slot] = state.current
                    save()
                # Fully completed earlier checkpoints are never reloaded under later weights.
                for row in validation:
                    if row.run_id not in state.completed:
                        if state.current != state.snapshots[slot]:
                            raise ValueError("checkpoint validation identity changed")
                        await sample(row)
        state.phase = "finished"
        save()
        return state

    finally:
        lease.close()

"""Injected offline-testable coordinator; no built-in provider factory or SDK session."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_rollout import (
    Digest,
    LearningGroup,
    LearningRollout,
    PolicyBinding,
    assemble_group,
    load_rollout,
)
from minidsh_rl.learning_schedule import (
    EXTRA_COST,
    GROUP_CLOSE_COST,
    GROUP_TRAIN_COST,
    INITIAL_COST,
    RESUME_COST,
    RUN_COST,
    STAGED_MAX_RESUMES,
    ExecutionKind,
    FrozenRecord,
    LearningPlan,
    QueueRow,
    atomic_record,
    digest,
    load_plan,
    resume_limit,
)
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.trainer import normalized_batch
from minidsh_rl.transaction import UpdateJournal

MIN_MIXED_GROUPS = 2
MIN_READY_SUCCESSES = 4
MAX_READY_SUCCESSES = 12
MIN_FEEDBACK_GROUPS = 2


class Checkpoint(FrozenRecord):
    execution_kind: ExecutionKind
    sampler_uri: str
    optimizer_uri: str
    policy: PolicyBinding
    preparation_spent: Decimal = Field(default=Decimal(0), ge=0, le=EXTRA_COST)


class ResourcePort(Protocol):
    @property
    def available(self) -> Decimal: ...
    @property
    def committed(self) -> Decimal: ...
    @property
    def has_pending(self) -> bool: ...


class RuntimePort(Protocol):
    @property
    def execution_kind(self) -> ExecutionKind: ...
    async def initialize(self, directory: Path) -> Checkpoint: ...
    async def restore(self, before: Checkpoint, directory: Path) -> None: ...
    async def rollout(
        self, case: LearningCase, row: QueueRow, before: Checkpoint, directory: Path
    ) -> LearningRollout: ...
    async def feedback(self, rollout: LearningRollout, directory: Path) -> bool: ...
    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, directory: Path
    ) -> None: ...


Reader = Callable[[LearningCase, Path, PolicyBinding], LearningRollout]


class ControlFact(FrozenRecord):
    name: str
    key: str
    occurred_at: str


class ExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    plan_sha256: Digest
    phase: Literal[
        "prepared",
        "running",
        "readiness_failed",
        "cancelled",
        "finished",
        "stopped",
        "awaiting_settlement",
    ] = "prepared"
    rows: dict[str, Literal["not_run", "started", "completed"]]
    row_artifacts: dict[str, dict[str, Digest]] = Field(default_factory=dict)
    update_artifacts: dict[str, dict[str, Digest]] = Field(default_factory=dict)
    initial_sha256: Digest | None = None
    initial_artifacts: dict[str, Digest] = Field(default_factory=dict)
    committed_groups: list[str] = Field(default_factory=list)
    initial: Checkpoint | None = None
    current: Checkpoint | None = None
    pending_kind: Literal["initialize", "resume", "rollout", "update"] | None = None
    pending_key: str = ""
    resumes: int = Field(default=0, ge=0, le=STAGED_MAX_RESUMES, strict=True)
    extra_spent: Decimal = Field(default=Decimal("0"), ge=0, le=EXTRA_COST)
    readiness_sha256: Digest | None = None
    start_committed: Decimal | None = None
    waiting_committed: Decimal | None = Field(default=None, ge=0)
    history: list[ControlFact] = Field(default_factory=list)


class GroupRecord(FrozenRecord):
    before: Checkpoint
    run_ids: tuple[str, ...]
    datum_sha256: Digest
    has_signal: bool
    input_sha256: dict[str, Digest]


class UpdateReceipt(FrozenRecord):
    group_sha256: Digest
    before: Checkpoint
    after: Checkpoint
    updated: bool
    journal_sha256: Digest


class ReadinessRecord(FrozenRecord):
    initial: Checkpoint
    mixed_groups: int
    successes: int
    feedback_groups: tuple[str, ...]
    input_sha256: dict[str, Digest]


def group_datum_hash(group: LearningGroup) -> str:
    """Match the existing TrainerV2 journal's measured sample_mean normalization."""
    if group.batch is None:
        text = json.dumps({"run_ids": group.run_ids, "no_assistant_tokens": True}, sort_keys=True)
    else:
        batch = normalized_batch(group.batch, "sample_mean")
        text = json.dumps([d.model_dump(mode="json") for d in batch.datums], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _checkpoint(value: Checkpoint, kind: ExecutionKind) -> None:
    prefix = "trio://" if kind == "provider" else "contract://"
    if (
        value.execution_kind != kind
        or not value.sampler_uri.startswith(prefix)
        or not value.optimizer_uri.startswith(prefix)
    ):
        raise ValueError("checkpoint source/URI mismatch")


def _journal_path(directory: Path, key: str) -> Path:
    return directory / f"update_{int(key.rsplit('_', maxsplit=1)[-1]):03}.json"


def write_update_receipt(  # noqa: PLR0913 - explicit confirmed checkpoint boundary
    directory: Path,
    key: str,
    group: LearningGroup,
    before: Checkpoint,
    after: Checkpoint,
    *,
    updated: bool,
) -> None:
    """Adapter calls only after confirmed optimizer state and matching sampler save."""
    record = GroupRecord.model_validate_json((directory / "group.json").read_bytes())
    if (
        record.before != before
        or record.run_ids != group.run_ids
        or record.datum_sha256 != group_datum_hash(group)
    ):
        raise ValueError("group receipt identity mismatch")
    journal = _journal_path(directory, key)
    atomic_record(
        directory / "receipt.json",
        UpdateReceipt(
            group_sha256=digest(directory / "group.json"),
            before=before,
            after=after,
            updated=updated,
            journal_sha256=digest(journal),
        ),
        create=True,
    )


def _files(root: Path, directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in control artifacts")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def _verify_files(root: Path, files: dict[str, str]) -> None:
    for name, sha in files.items():
        path = root / name
        if (
            not path.resolve().is_relative_to(root.resolve())
            or path.is_symlink()
            or not path.is_file()
            or digest(path) != sha
        ):
            raise ValueError("completed input artifact changed")


def _initial_inputs(root: Path) -> dict[str, str]:
    return {
        "initial.json": digest(root / "initial.json"),
        **_files(root, root / "initial-sdk"),
        **_files(root, root / "policies"),
    }


def validate_state(plan: LearningPlan, state: ExecutionState, plan_sha256: str) -> None:
    """Check persisted scheduling invariants before considering any external call."""
    committed = set(state.committed_groups)
    train_groups = sorted({r.group for r in plan.rows if r.phase == "train"})
    completed = {key for key, value in state.rows.items() if value == "completed"}
    if state.plan_sha256 != plan_sha256 or set(state.rows) != {r.run_id for r in plan.rows}:
        raise ValueError("execution state/plan mismatch")
    if state.resumes > resume_limit(plan):
        raise ValueError("resume allowance changed")
    if (state.phase == "awaiting_settlement") != (state.waiting_committed is not None):
        raise ValueError("funding wait receipt mismatch")
    if state.phase == "awaiting_settlement" and (
        plan.funding_policy != "confirmed_stage_v1"
        or state.pending_kind is not None
        or not _boundary(plan, state)
    ):
        raise ValueError("funding wait requires a confirmed boundary without pending calls")
    if (
        state.committed_groups != train_groups[: len(state.committed_groups)]
        or set(state.update_artifacts) != committed
        or set(state.row_artifacts) != completed
    ):
        raise ValueError("execution receipt coverage mismatch")
    if any(
        state.rows[r.run_id] != "completed"
        for r in plan.rows
        if r.phase == "train" and r.group in committed
    ):
        raise ValueError("committed group has incomplete declared rows")
    if (state.initial is None) != (state.current is None) or (committed and state.initial is None):
        raise ValueError("missing confirmed checkpoint")
    if state.initial is not None and not state.initial_artifacts:
        raise ValueError("missing initial input receipts")
    if state.phase == "finished" and (
        len(completed) != len(plan.rows) or len(committed) != len(train_groups)
    ):
        raise ValueError("finished state is incomplete")


def remaining_allocation(plan: LearningPlan, state: ExecutionState) -> Decimal:
    runs = sum(state.rows[r.run_id] != "completed" for r in plan.rows)
    groups = {r.group for r in plan.rows if r.phase == "train"} - set(state.committed_groups)
    return (
        runs * RUN_COST
        + len(groups) * (GROUP_TRAIN_COST + GROUP_CLOSE_COST)
        + (INITIAL_COST if state.initial is None else Decimal(0))
        + EXTRA_COST
        - state.extra_spent
    )


def _stage_rows(plan: LearningPlan, state: ExecutionState) -> tuple[QueueRow, ...]:
    readiness = tuple(r for r in plan.rows if r.phase == "readiness")
    if state.readiness_sha256 is None:
        return readiness
    for key in sorted({r.group for r in plan.rows if r.phase == "train"}):
        if key not in state.committed_groups:
            return tuple(r for r in plan.rows if r.phase == "train" and r.group == key)
    return tuple(r for r in plan.rows if r.phase == "test")


def _boundary(plan: LearningPlan, state: ExecutionState) -> bool:
    rows = _stage_rows(plan, state)
    count = sum(state.rows[r.run_id] == "completed" for r in rows)
    # Readiness feedback is local and can be recomputed from the confirmed rows.
    return count == 0 or (rows[0].phase == "readiness" and count == len(rows))


def next_stage_allocation(plan: LearningPlan, state: ExecutionState) -> Decimal:
    """Whole next segment, including shared remaining preparation/resume allowance."""
    rows = _stage_rows(plan, state)
    runs = sum(state.rows[r.run_id] != "completed" for r in rows)
    training = GROUP_TRAIN_COST + GROUP_CLOSE_COST if rows[0].phase == "train" else Decimal(0)
    return (
        runs * RUN_COST
        + training
        + (INITIAL_COST if state.initial is None else Decimal(0))
        + EXTRA_COST
        - state.extra_spent
    )


class _FundingWaitError(ValueError):
    """Only insufficient segment capacity; unknown or invalid budgets never use it."""


def _validate_resources(plan: LearningPlan, state: ExecutionState, budget: ResourcePort) -> None:
    if not budget.available.is_finite() or not budget.committed.is_finite() or budget.committed < 0:
        raise ValueError("invalid budget snapshot")
    if budget.has_pending:
        raise ValueError("pending financial operation requires audit")
    if budget.committed > Decimal("50") or budget.available < 0:
        raise ValueError("cumulative budget exceeded")
    if state.start_committed is not None and budget.committed < state.start_committed:
        raise ValueError("budget baseline changed")
    if (
        state.start_committed is not None
        and budget.committed - state.start_committed + remaining_allocation(plan, state)
        > plan.required_cny
    ):
        raise ValueError("stage budget would be exceeded")


def _admit(plan: LearningPlan, state: ExecutionState, budget: ResourcePort) -> None:
    _validate_resources(plan, state, budget)
    required = (
        next_stage_allocation(plan, state)
        if plan.funding_policy == "confirmed_stage_v1"
        else remaining_allocation(plan, state)
    )
    if budget.available < required:
        if plan.funding_policy == "confirmed_stage_v1":
            raise _FundingWaitError("next confirmed stage budget does not fit")
        raise ValueError("full remaining stage budget does not fit")


def _save(root: Path, state: ExecutionState, name: str, key: str = "") -> None:
    state.history.append(ControlFact(name=name, key=key, occurred_at=datetime.now(UTC).isoformat()))
    atomic_record(root / "state.json", state)


def _accept_update(root: Path, state: ExecutionState, key: str) -> None:
    folder = root / "updates" / key
    if not (folder / "receipt.json").is_file():
        raise ValueError("unknown update outcome; do not replay")
    record = GroupRecord.model_validate_json((folder / "group.json").read_bytes())
    receipt = UpdateReceipt.model_validate_json((folder / "receipt.json").read_bytes())
    journal_path = _journal_path(folder, key)
    journal = UpdateJournal.load(journal_path)
    _verify_files(root, record.input_sha256)
    _checkpoint(receipt.after, receipt.before.execution_kind)
    if (
        receipt.group_sha256 != digest(folder / "group.json")
        or receipt.before != record.before
        or receipt.before != state.current
        or receipt.journal_sha256 != digest(journal_path)
        or journal.datum_hash != record.datum_sha256
        or not journal.safe_to_resume
        or receipt.updated != record.has_signal
        or receipt.after.policy.tokenizer_sha256 != receipt.before.policy.tokenizer_sha256
    ):
        raise ValueError("confirmed update receipt mismatch")
    if (
        receipt.updated
        and (journal.state != "checkpointed" or journal.checkpoint != receipt.after.optimizer_uri)
    ) or (not receipt.updated and (journal.state != "skipped" or receipt.after != receipt.before)):
        raise ValueError("optimizer checkpoint not confirmed")
    state.current = receipt.after
    state.update_artifacts[key] = _files(root, folder)
    state.committed_groups.append(key)
    state.pending_kind = None
    state.pending_key = ""
    _save(root, state, "update_committed", key)


class _Controller:
    def __init__(
        self,
        root: Path,
        plan: LearningPlan,
        state: ExecutionState,
        budget: ResourcePort,
        reader: Reader,
    ) -> None:
        self.root, self.plan, self.state, self.budget, self.reader = (
            root,
            plan,
            state,
            budget,
            reader,
        )
        self.cases = {c.task.case_id: c for c in learning_cases()}

    def _check_row(self, row: QueueRow, value: LearningRollout, before: Checkpoint) -> None:
        if (
            value.run_id != row.run_id
            or value.seed != row.seed
            or value.case != self.cases[row.case_id]
            or value.policy != before.policy
        ):
            raise ValueError("queue/result identity mismatch")

    def recover(self) -> None:
        state = self.state
        if state.pending_kind == "update":
            _accept_update(self.root, state, state.pending_key)
        elif state.pending_kind == "initialize" and (self.root / "initial.json").is_file():
            value = Checkpoint.model_validate_json((self.root / "initial.json").read_bytes())
            _checkpoint(value, self.plan.execution_kind)
            state.initial = state.current = value
            state.initial_sha256 = digest(self.root / "initial.json")
            state.initial_artifacts = _initial_inputs(self.root)
            state.extra_spent = value.preparation_spent
            state.pending_kind = None
            _save(self.root, state, "initial_recovered")
        elif state.pending_kind == "rollout" and state.current is not None:
            row = next(r for r in self.plan.rows if r.run_id == state.pending_key)
            before = state.initial if row.policy == "base" else state.current
            if before is None:
                raise ValueError("missing initial policy")
            rollout = self.reader(
                self.cases[row.case_id], self.root / "rollouts" / row.run_id, before.policy
            )
            self._check_row(row, rollout, before)
            self._complete(row, rollout)
        elif state.pending_kind is not None:
            raise ValueError("unknown pending operation; do not replay")

    def _complete(self, row: QueueRow, value: LearningRollout) -> None:
        self.state.rows[row.run_id] = "completed"
        self.state.row_artifacts[row.run_id] = _files(
            self.root, self.root / "rollouts" / row.run_id
        )
        self.state.pending_kind = None
        self.state.pending_key = ""
        if value.budget_violations:
            self.state.phase = "stopped"
        elif value.status == "cancelled":
            self.state.phase = "cancelled"
        _save(self.root, self.state, "rollout_completed", row.run_id)
        if self.state.phase in {"stopped", "cancelled"}:
            raise ValueError("budget/cancellation stops queue; all declared rows retained")

    async def row(self, row: QueueRow, runtime: RuntimePort, before: Checkpoint) -> LearningRollout:
        folder = self.root / "rollouts" / row.run_id
        if self.state.rows[row.run_id] == "completed":
            value = self.reader(self.cases[row.case_id], folder, before.policy)
        else:
            _admit(self.plan, self.state, self.budget)
            self.state.rows[row.run_id] = "started"
            self.state.pending_kind, self.state.pending_key = "rollout", row.run_id
            _save(self.root, self.state, "rollout_started", row.run_id)
            value = await runtime.rollout(self.cases[row.case_id], row, before, folder)
            self._check_row(row, value, before)
            self._complete(row, value)
        self._check_row(row, value, before)
        return value

    async def readiness(self, runtime: RuntimePort) -> bool:
        if self.state.initial is None:
            raise ValueError("missing initial policy")
        values = [
            await self.row(row, runtime, self.state.initial)
            for row in self.plan.rows
            if row.phase == "readiness"
        ]
        rewards: dict[str, list[bool]] = {}
        recovered: set[str] = set()
        for row, value in zip(
            (r for r in self.plan.rows if r.phase == "readiness"), values, strict=True
        ):
            rewards.setdefault(row.group, []).append(value.success)
        mixed = sum(len(set(group)) > 1 for group in rewards.values())
        successes = sum(value.success for value in values)
        path = self.root / "readiness.json"
        if path.exists():
            proof = ReadinessRecord.model_validate_json(path.read_bytes())
            _verify_files(self.root, proof.input_sha256)
            if (
                proof.initial != self.state.initial
                or proof.mixed_groups != mixed
                or proof.successes != successes
                or (
                    self.state.readiness_sha256 is not None
                    and digest(path) != self.state.readiness_sha256
                )
            ):
                raise ValueError("readiness proof changed")
            recovered.update(proof.feedback_groups)
        else:
            for value in values:
                if value.success and await runtime.feedback(
                    value, self.root / "analysis" / value.run_id
                ):
                    recovered.add(value.case.family_group)
            inputs = {
                p: sha
                for row in self.plan.rows
                if row.phase == "readiness"
                for p, sha in self.state.row_artifacts[row.run_id].items()
            }
            inputs.update(_files(self.root, self.root / "analysis"))
            atomic_record(
                path,
                ReadinessRecord(
                    initial=self.state.initial,
                    mixed_groups=mixed,
                    successes=successes,
                    feedback_groups=tuple(sorted(recovered)),
                    input_sha256=inputs,
                ),
                create=True,
            )
        self.state.readiness_sha256 = digest(path)
        allowed = (
            mixed >= MIN_MIXED_GROUPS
            and MIN_READY_SUCCESSES <= successes <= MAX_READY_SUCCESSES
            and len(recovered) >= MIN_FEEDBACK_GROUPS
        )
        if not allowed:
            self.state.phase = "readiness_failed"
            _save(self.root, self.state, "readiness_rejected")
        else:
            _save(self.root, self.state, "readiness_confirmed")
        return allowed

    async def train(self, runtime: RuntimePort) -> None:
        keys = sorted({r.group for r in self.plan.rows if r.phase == "train"})
        for key in keys:
            if key in self.state.committed_groups:
                continue
            before = self.state.current
            if before is None:
                raise ValueError("missing current checkpoint")
            values = tuple(
                [
                    await self.row(row, runtime, before)
                    for row in self.plan.rows
                    if row.phase == "train" and row.group == key
                ]
            )
            group = assemble_group(values)
            folder = self.root / "updates" / key
            folder.mkdir(parents=True, exist_ok=True)
            inputs = {
                path: sha
                for value in values
                for path, sha in self.state.row_artifacts[value.run_id].items()
            }
            record = GroupRecord(
                before=before,
                run_ids=group.run_ids,
                datum_sha256=group_datum_hash(group),
                has_signal=group.batch is not None and group.batch.has_signal,
                input_sha256=inputs,
            )
            if (folder / "group.json").exists():
                if GroupRecord.model_validate_json((folder / "group.json").read_bytes()) != record:
                    raise ValueError("prepared group changed")
            else:
                atomic_record(folder / "group.json", record, create=True)
            _admit(self.plan, self.state, self.budget)
            self.state.pending_kind, self.state.pending_key = "update", key
            _save(self.root, self.state, "update_started", key)
            await runtime.update(key, group, before, folder)
            _accept_update(self.root, self.state, key)


async def _start_runtime(
    directory: Path,
    plan: LearningPlan,
    state: ExecutionState,
    budget: ResourcePort,
    runtime: RuntimePort,
) -> None:
    if state.initial is None:
        state.start_committed = budget.committed
        state.pending_kind = "initialize"
        _save(directory, state, "initial_started")
        value = await runtime.initialize(directory)
        recorded = Checkpoint.model_validate_json((directory / "initial.json").read_bytes())
        _checkpoint(value, plan.execution_kind)
        if value != recorded:
            raise ValueError("initial receipt mismatch")
        state.initial = state.current = value
        state.initial_sha256 = digest(directory / "initial.json")
        state.initial_artifacts = _initial_inputs(directory)
        state.extra_spent = value.preparation_spent
        state.pending_kind = None
        _save(directory, state, "initial_committed")
    else:
        if state.resumes >= resume_limit(plan) or state.current is None:
            raise ValueError("resume allowance exhausted")
        state.resumes += 1
        state.pending_kind = "resume"
        _save(directory, state, "resume_started")
        await runtime.restore(state.current, directory)
        state.extra_spent += RESUME_COST
        state.pending_kind = None
        _save(directory, state, "resume_confirmed")


def _entry_ready(
    plan: LearningPlan, state: ExecutionState, budget: ResourcePort, *, resume: bool
) -> bool:
    if state.phase == "stopped" or (state.initial is not None and not resume):
        raise ValueError("explicit safe resume required")
    if state.phase == "awaiting_settlement":
        if not resume:
            raise ValueError("explicit safe resume required")
        _validate_resources(plan, state, budget)
        if state.waiting_committed is not None and budget.committed >= state.waiting_committed:
            return False
    if state.initial is not None and (
        state.resumes >= resume_limit(plan) or state.extra_spent + RESUME_COST > EXTRA_COST
    ):
        raise ValueError("resume allowance exhausted")
    _admit(plan, state, budget)
    if plan.execution_kind == "provider" and plan.data_evidence is None:
        raise ValueError("provider data gate missing")
    return True


def _wait_for_settlement(
    directory: Path, plan: LearningPlan, state: ExecutionState, budget: ResourcePort
) -> ExecutionState:
    if state.pending_kind is not None or not _boundary(plan, state):
        state.phase = "stopped"
        state.waiting_committed = None
        _save(directory, state, "unexpected_mid_stage_shortfall")
        raise ValueError("funding shortfall outside a confirmed boundary") from None
    state.phase = "awaiting_settlement"
    state.waiting_committed = budget.committed
    _save(directory, state, "funding_wait_started")
    return state


async def execute(  # noqa: PLR0913 - explicit injected execution and preflight boundaries
    directory: Path,
    budget: ResourcePort,
    factory: Callable[[], RuntimePort],
    *,
    reader: Reader = load_rollout,
    resume: bool = False,
    before_factory: Callable[[], Awaitable[None]] | None = None,
) -> ExecutionState:
    """One leased controller; real callers must additionally own the financial lease."""
    lease = FileLease(directory / "controller.lock")
    try:
        plan = load_plan(directory)
        path = directory / "state.json"
        state = (
            ExecutionState.model_validate_json(path.read_bytes())
            if path.exists()
            else ExecutionState(
                plan_sha256=digest(directory / "plan.json"),
                rows={r.run_id: "not_run" for r in plan.rows},
            )
        )
        validate_state(plan, state, digest(directory / "plan.json"))
        for files in state.row_artifacts.values():
            _verify_files(directory, files)
        for files in state.update_artifacts.values():
            _verify_files(directory, files)
        _verify_files(directory, state.initial_artifacts)
        if state.initial is not None and state.initial_sha256 != digest(directory / "initial.json"):
            raise ValueError("initial artifact changed")
        control = _Controller(directory, plan, state, budget, reader)
        control.recover()
        if state.phase in {"finished", "readiness_failed"}:
            return state
        if not _entry_ready(plan, state, budget, resume=resume):
            return state
        if before_factory is not None:
            await before_factory()
            load_plan(directory)
            _admit(plan, state, budget)
        state.waiting_committed = None
        state.phase = "running"
        _save(directory, state, "stage_admitted")
        runtime = factory()
        if runtime.execution_kind != plan.execution_kind:
            raise ValueError("runtime provenance mismatch")
        await _start_runtime(directory, plan, state, budget, runtime)
        state.phase = "running"
        if not await control.readiness(runtime):
            return state
        await control.train(runtime)
        for row in (r for r in plan.rows if r.phase == "test"):
            before = state.initial if row.policy == "base" else state.current
            if before is None:
                raise ValueError("missing final evaluation policy")
            await control.row(row, runtime, before)
        state.phase = "finished"
        _save(directory, state, "study_finished")
        return state
    except _FundingWaitError:
        return _wait_for_settlement(directory, plan, state, budget)
    finally:
        lease.close()

"""Positive-control v1 execution controller with an injected runtime.

This module owns the baseline -> selection -> training -> evaluation state machine.
It never constructs an SDK client and never spends money on its own; a provider
adapter must be injected explicitly and remains responsible for financial leasing.
"""

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import Checkpoint, GroupRecord, group_datum_hash
from minidsh_rl.learning_rollout import LearningGroup, LearningRollout, assemble_group
from minidsh_rl.learning_schedule import ExecutionKind, atomic_record, digest
from minidsh_rl.positive_control import (
    CHECKPOINTS,
    FAIL_MAX,
    FAIL_MIN_UPDATES,
    PASS_DELTA,
    PASS_MIN,
    PASS_TASK_MIN,
    BaselinePlan,
    PositiveControlPlan,
    PositiveRow,
    load_baseline_plan,
    load_plan,
    make_baseline_plan,
    make_plan,
    positive_cases,
    select_tasks,
)

ZERO_SIGNAL_LIMIT = 10


class PositiveRuntime(Protocol):
    """Boundary for a scripted fixture or a real provider adapter."""

    @property
    def execution_kind(self) -> ExecutionKind: ...

    async def initialize(self, directory: Path) -> Checkpoint: ...

    async def sample(
        self,
        case: LearningCase,
        row: PositiveRow,
        before: Checkpoint,
        output: Path,
    ) -> LearningRollout: ...

    async def update(
        self,
        key: str,
        group: LearningGroup,
        before: Checkpoint,
        output: Path,
    ) -> Checkpoint: ...


class PositiveControlState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    baseline_plan_sha256: str
    phase: Literal["prepared", "baseline", "selected", "training", "finished", "stopped"] = (
        "prepared"
    )
    kind: ExecutionKind
    initial: Checkpoint | None = None
    current: Checkpoint | None = None
    pending: Literal["baseline", "train", "eval", "update"] | None = None
    pending_key: str = ""
    results: dict[str, bool] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    snapshots: dict[int, Checkpoint] = Field(default_factory=dict)
    zero_streak: int = 0
    selection_evidence_sha256: str | None = None
    plan_sha256: str | None = None
    selected: list[str] = Field(default_factory=list)
    final: dict[str, object] | None = None


def prepare_positive_control(root: Path) -> BaselinePlan:
    """Freeze the baseline queue; no runtime, client or spend is created."""
    if root.exists():
        raise ValueError("positive-control directory already exists")
    root.mkdir(parents=True)
    plan = make_baseline_plan()
    atomic_record(root / "baseline-plan.json", plan, create=True)
    return plan


def _check_row(row: PositiveRow, value: LearningRollout, before: Checkpoint) -> None:
    if (
        value.run_id != row.run_id
        or value.seed != row.seed
        or value.case.task.case_id != row.case_id
        or value.policy != before.policy
        or value.budget_violations
        or value.status == "cancelled"
    ):
        raise ValueError("trajectory identity/budget/cancellation failure")


def _save(root: Path, state: PositiveControlState) -> None:
    atomic_record(root / "state.json", state)


def _load_state(root: Path, kind: ExecutionKind) -> PositiveControlState:
    path = root / "state.json"
    state = (
        PositiveControlState.model_validate_json(path.read_bytes())
        if path.exists()
        else PositiveControlState(
            baseline_plan_sha256=digest(root / "baseline-plan.json"),
            kind=kind,
        )
    )
    if state.baseline_plan_sha256 != digest(root / "baseline-plan.json"):
        raise ValueError("baseline plan changed")
    if state.kind != kind:
        raise ValueError("runtime provenance changed")
    if state.pending is not None:
        raise ValueError("in-flight operation; explicit recovery required")
    plan_path = root / "plan.json"
    if plan_path.exists():
        plan = load_plan(plan_path)
        if state.plan_sha256 != digest(plan_path) or tuple(state.selected) != plan.selected:
            raise ValueError("frozen plan changed")
    return state


async def _sample_rows(  # noqa: PLR0913, PLR0917 - explicit execution boundary
    root: Path,
    state: PositiveControlState,
    runtime: PositiveRuntime,
    rows: tuple[PositiveRow, ...],
    before: Checkpoint,
    phase: Literal["baseline", "train", "eval"],
) -> tuple[LearningRollout, ...]:
    cases = positive_cases()
    values = []
    for row in rows:
        if row.run_id in state.completed:
            continue
        state.pending, state.pending_key = phase, row.run_id
        _save(root, state)
        value = await runtime.sample(
            cases[row.case_id], row, before, root / "rollouts" / row.run_id
        )
        _check_row(row, value, before)
        state.results[row.run_id] = value.success
        state.completed.append(row.run_id)
        state.pending, state.pending_key = None, ""
        _save(root, state)
        values.append(value)
    return tuple(values)


async def _baseline(
    root: Path,
    state: PositiveControlState,
    runtime: PositiveRuntime,
    plan: BaselinePlan,
) -> PositiveControlPlan:
    if state.initial is None:
        raise ValueError("initial checkpoint missing")
    state.phase = "baseline"
    _save(root, state)
    await _sample_rows(root, state, runtime, plan.rows, state.initial, "baseline")
    results_path = root / "baseline-results.json"
    results = {row.run_id: state.results[row.run_id] for row in plan.rows}
    if results_path.exists():
        if json.loads(results_path.read_text()) != results:
            raise ValueError("baseline results changed")
    else:
        results_path.write_text(json.dumps(results, indent=2))
    evidence = digest(results_path)
    selection = select_tasks(plan.rows, results)
    frozen = make_plan(selection, selection_evidence_sha256=evidence)
    plan_path = root / "plan.json"
    if plan_path.exists():
        if load_plan(plan_path) != frozen:
            raise ValueError("frozen positive-control plan changed")
    else:
        atomic_record(plan_path, frozen, create=True)
    state.selection_evidence_sha256 = evidence
    state.plan_sha256 = digest(plan_path)
    state.selected = list(frozen.selected)
    state.phase = "selected"
    _save(root, state)
    return frozen


async def _training(  # noqa: PLR0912, PLR0915 - explicit leased state machine
    root: Path,
    state: PositiveControlState,
    runtime: PositiveRuntime,
    plan: PositiveControlPlan,
) -> None:
    state.phase = "training"
    _save(root, state)
    for group_index in range(len({row.group for row in plan.train_rows})):
        key = f"train_{group_index:02}"
        trained = key not in state.groups
        if trained:
            rows = tuple(row for row in plan.train_rows if row.group == key)
            if any(row.run_id in state.completed for row in rows):
                raise ValueError("partially completed training group; do not replay")
            if state.current is None:
                raise ValueError("current checkpoint missing")
            before = state.current
            values = await _sample_rows(root, state, runtime, rows, before, "train")
            if len(values) != len(rows):
                raise ValueError("training group not fully sampled")
            group = assemble_group(values)
            folder = root / "updates" / key
            folder.mkdir(parents=True, exist_ok=True)
            record = GroupRecord(
                before=before,
                run_ids=group.run_ids,
                datum_sha256=group_datum_hash(group),
                has_signal=group.batch is not None and group.batch.has_signal,
                input_sha256={},
            )
            if (folder / "group.json").exists():
                if GroupRecord.model_validate_json((folder / "group.json").read_bytes()) != record:
                    raise ValueError("prepared group changed")
            else:
                atomic_record(folder / "group.json", record, create=True)
            state.pending, state.pending_key = "update", key
            _save(root, state)
            after = await runtime.update(key, group, before, folder)
            state.current = after
            state.groups.append(key)
            if record.has_signal:
                state.updated.append(key)
                state.zero_streak = 0
            else:
                state.zero_streak += 1
            state.pending, state.pending_key = None, ""
            _save(root, state)
        completed = group_index + 1
        if completed in CHECKPOINTS[1:]:
            if completed not in state.snapshots:
                if not trained:
                    raise ValueError("missing checkpoint snapshot; cannot resume")
                if state.current is None:
                    raise ValueError("checkpoint missing for evaluation")
                state.snapshots[completed] = state.current
                _save(root, state)
            await _sample_rows(
                root,
                state,
                runtime,
                tuple(row for row in plan.eval_rows if row.checkpoint == completed),
                state.snapshots[completed],
                "eval",
            )
        if state.zero_streak >= ZERO_SIGNAL_LIMIT:
            state.phase = "stopped"
            state.final = final_report(plan, state, reason="zero-signal streak")
            (root / "final-report.json").write_text(json.dumps(state.final, indent=2))
            _save(root, state)
            return
    state.phase = "finished"
    state.final = final_report(plan, state)
    (root / "final-report.json").write_text(json.dumps(state.final, indent=2))
    _save(root, state)


def final_report(
    plan: PositiveControlPlan, state: PositiveControlState, *, reason: str = ""
) -> dict[str, object]:
    baseline_rows = tuple(row for row in plan.baseline_rows if row.case_id in plan.selected)
    final_rows = tuple(row for row in plan.eval_rows if row.checkpoint == CHECKPOINTS[-1])
    baseline_measured = all(row.run_id in state.results for row in baseline_rows)
    final_measured = all(row.run_id in state.results for row in final_rows)
    baseline = sum(state.results.get(row.run_id, False) for row in baseline_rows)
    final = sum(state.results.get(row.run_id, False) for row in final_rows)
    per_task = {
        case_id: {
            "baseline": sum(
                state.results.get(row.run_id, False)
                for row in baseline_rows
                if row.case_id == case_id
            ),
            "final": sum(
                state.results.get(row.run_id, False) for row in final_rows if row.case_id == case_id
            ),
        }
        for case_id in plan.selected
    }
    updates = len(state.updated)
    if not final_measured:
        status = "inconclusive"
        reason = reason or "final checkpoint not fully measured"
    elif (
        final >= PASS_MIN
        and final - baseline >= PASS_DELTA
        and all(item["final"] >= PASS_TASK_MIN for item in per_task.values())
    ):
        status = "pass"
        reason = reason or "criteria met"
    elif final <= FAIL_MAX and updates >= FAIL_MIN_UPDATES:
        status = "fail"
        reason = reason or "final below threshold with enough updates"
    else:
        status = "inconclusive"
        reason = reason or "between pass and fail thresholds"
    return {
        "schema_version": 1,
        "protocol": "positive_control_v1",
        "status": status,
        "reason": reason,
        "baseline_measured": baseline_measured,
        "final_measured": final_measured,
        "baseline_successes": baseline if baseline_measured else None,
        "final_successes": final if final_measured else None,
        "delta": final - baseline if baseline_measured and final_measured else None,
        "per_task": per_task,
        "updates": updates,
        "zero_signal_groups": len(state.groups) - updates,
        "completed_rows": len(state.completed),
        "declared_rows": len(plan.baseline_rows) + len(plan.train_rows) + len(plan.eval_rows),
        "criterion": {
            "pass_min": PASS_MIN,
            "pass_delta": PASS_DELTA,
            "pass_task_min": PASS_TASK_MIN,
            "fail_max": FAIL_MAX,
            "fail_min_updates": FAIL_MIN_UPDATES,
        },
    }


async def execute_positive_control(
    root: Path,
    runtime: PositiveRuntime,
    *,
    resume: bool = False,
) -> PositiveControlState:
    """Run baseline, freeze the selected plan, then train and evaluate."""
    baseline_plan = load_baseline_plan(root / "baseline-plan.json")
    state = _load_state(root, runtime.execution_kind)
    if state.phase in {"finished", "stopped"}:
        return state
    if (root / "state.json").exists() and not resume:
        raise ValueError("explicit resume required")
    if state.initial is None:
        if (root / "initial.json").exists():
            raise ValueError("initial checkpoint already exists")
        state.initial = state.current = await runtime.initialize(root)
        recorded = Checkpoint.model_validate_json((root / "initial.json").read_bytes())
        if recorded != state.initial:
            raise ValueError("initial receipt mismatch")
        _save(root, state)
    elif not resume:
        raise ValueError("explicit resume required")
    plan = await _baseline(root, state, runtime, baseline_plan)
    await _training(root, state, runtime, plan)
    return state

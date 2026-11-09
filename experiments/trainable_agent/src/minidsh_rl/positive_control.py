"""Frozen positive-control v1 queue: three tasks, six epochs, four checkpoints.

The baseline phase is pre-declared for the first three candidates per family. Selection
uses only that baseline evidence and the frozen fallback order; the training and
evaluation rows are then generated deterministically and must match a reload.
"""

# ruff: noqa: PERF401 - explicit loops follow the frozen scientific queue order

import hashlib
import random
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_rollout import _case_digest
from minidsh_rl.learning_schedule import (
    EXTRA_COST,
    GROUP_CLOSE_COST,
    GROUP_TRAIN_COST,
    INITIAL_COST,
    RUN_COST,
)

Family = Literal["match_rank", "dependency_batches", "table_join"]
FAMILIES: tuple[Family, ...] = ("match_rank", "dependency_batches", "table_join")
CANDIDATES: dict[Family, tuple[str, ...]] = {
    "match_rank": (
        "curve_v4_path_router_02",
        "curve_v4_path_router_01",
        "curve_v4_path_router_03",
        "curve_v4_path_router_00",
    ),
    "dependency_batches": (
        "curve_v4_dependency_batches_01",
        "curve_v4_dependency_batches_03",
        "curve_v4_dependency_batches_02",
        "curve_v4_dependency_batches_00",
    ),
    "table_join": (
        "curve_v4_table_join_02",
        "curve_v4_table_join_00",
        "curve_v4_table_join_01",
        "curve_v4_table_join_03",
    ),
}
BASELINE_ROUNDS = 3
GROUP_SIZE = 4
EPOCHS = 6
CHECKPOINTS = (0, 6, 12, 18)
EVAL_SEEDS = 8
SHUFFLE_SEED = 29
TRAIN_SEED_BASE = 971_000
EVAL_SEED_BASE = 981_000
BASELINE_MIN = 1
BASELINE_MAX = 4
PASS_MIN = 20
PASS_DELTA = 8
PASS_TASK_MIN = 5
FAIL_MAX = 16
FAIL_MIN_UPDATES = 8
RUN_ID = r"^[a-z][a-z0-9_]{0,63}$"


class FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PositiveRow(FrozenRecord):
    run_id: str = Field(pattern=RUN_ID)
    phase: Literal["baseline", "train", "eval"]
    family: Family
    case_id: str
    seed: int = Field(ge=0, strict=True)
    repeat: int = Field(ge=0, le=7, strict=True)
    round: int | None = Field(default=None, ge=0, le=2, strict=True)
    group: str
    checkpoint: int = Field(ge=0, le=18, strict=True)


class PositiveSelection(FrozenRecord):
    family: Family
    case_id: str
    round: int = Field(ge=0, le=2, strict=True)
    successes: int = Field(ge=0, le=8, strict=True)


class BaselinePlan(FrozenRecord):
    schema_version: Literal[1] = 1
    protocol: Literal["positive_control_v1"] = "positive_control_v1"
    rows: tuple[PositiveRow, ...]
    case_sha256: dict[str, str]


class PositiveControlPlan(FrozenRecord):
    schema_version: Literal[1] = 1
    protocol: Literal["positive_control_v1"] = "positive_control_v1"
    dataset_version: Literal["curve_dataset_v4"] = "curve_dataset_v4"
    families: tuple[str, ...]
    selected: tuple[str, ...]
    selection: tuple[PositiveSelection, ...]
    baseline_rows: tuple[PositiveRow, ...]
    train_rows: tuple[PositiveRow, ...]
    eval_rows: tuple[PositiveRow, ...]
    checkpoints: tuple[int, ...]
    group_size: Literal[4] = 4
    eval_seeds: Literal[8] = 8
    shuffle_seed: Literal[29] = 29
    train_seed_base: Literal[971000] = 971000
    eval_seed_base: Literal[981000] = 981000
    baseline_min: Literal[1] = 1
    baseline_max: Literal[4] = 4
    pass_min: Literal[20] = 20
    pass_delta: Literal[8] = 8
    pass_task_min: Literal[5] = 5
    fail_max: Literal[16] = 16
    fail_min_updates: Literal[8] = 8
    required_cny: Decimal
    case_sha256: dict[str, str]
    selection_evidence_sha256: str | None = None


class InfeasiblePositiveControlError(ValueError):
    """No candidate in a family met the pre-declared baseline window."""


def _case_family(case: LearningCase) -> Family:
    family = case.family_group
    if family not in FAMILIES:
        raise ValueError(f"unknown family group: {family}")
    return family


def positive_cases() -> dict[str, LearningCase]:
    cases = {c.task.case_id: c for c in curve_cases_v4() if c.split == "train"}
    for family in FAMILIES:
        for case_id in CANDIDATES[family]:
            if case_id not in cases:
                raise ValueError(f"missing positive-control case: {case_id}")
            if cases[case_id].family_group != family:
                raise ValueError(f"case {case_id} is not in {family}")
    return cases


def _candidate_case_sha256(cases: dict[str, LearningCase]) -> dict[str, str]:
    return {
        case_id: _case_digest(cases[case_id])
        for family in FAMILIES
        for case_id in CANDIDATES[family][:BASELINE_ROUNDS]
    }


def make_baseline_rows() -> tuple[PositiveRow, ...]:
    cases = positive_cases()
    rows = []
    for family_index, family in enumerate(FAMILIES):
        for round_index, case_id in enumerate(CANDIDATES[family][:BASELINE_ROUNDS]):
            for repeat in range(EVAL_SEEDS):
                rows.append(
                    PositiveRow(
                        run_id=f"pc1_base_{family_index}_{round_index}_{repeat}",
                        phase="baseline",
                        family=family,
                        case_id=case_id,
                        seed=EVAL_SEED_BASE + family_index * 100 + repeat,
                        repeat=repeat,
                        round=round_index,
                        group=f"base_{family_index}_{round_index}",
                        checkpoint=0,
                    )
                )
    if any(_case_family(cases[row.case_id]) != row.family for row in rows):
        raise ValueError("baseline family binding changed")
    return tuple(rows)


def make_baseline_plan() -> BaselinePlan:
    """Freeze the 72-row feasibility queue before any baseline sampling."""
    return BaselinePlan(
        rows=make_baseline_rows(),
        case_sha256=_candidate_case_sha256(positive_cases()),
    )


def load_baseline_plan(path: Path) -> BaselinePlan:
    plan = BaselinePlan.model_validate_json(path.read_bytes())
    if plan != make_baseline_plan():
        raise ValueError("positive-control baseline plan changed")
    return plan


def _baseline_counts(
    baseline_rows: tuple[PositiveRow, ...], results: Mapping[str, bool]
) -> dict[tuple[str, int], int]:
    missing = [row.run_id for row in baseline_rows if row.run_id not in results]
    if missing:
        raise ValueError(f"missing baseline results: {missing[:3]}")
    counts: dict[tuple[str, int], int] = {}
    for row in baseline_rows:
        if results[row.run_id]:
            key = (row.family, row.round or 0)
            counts[key] = counts.get(key, 0) + 1
    return counts


def select_tasks(
    baseline_rows: tuple[PositiveRow, ...], results: Mapping[str, bool]
) -> tuple[PositiveSelection, ...]:
    """Apply the frozen fallback order and the 1-4/8 feasibility window."""
    counts = _baseline_counts(baseline_rows, results)
    selected = []
    for family in FAMILIES:
        for round_index, case_id in enumerate(CANDIDATES[family][:BASELINE_ROUNDS]):
            successes = counts.get((family, round_index), 0)
            if BASELINE_MIN <= successes <= BASELINE_MAX:
                selected.append(
                    PositiveSelection(
                        family=family, case_id=case_id, round=round_index, successes=successes
                    )
                )
                break
        else:
            observed = [counts.get((family, index), 0) for index in range(BASELINE_ROUNDS)]
            raise InfeasiblePositiveControlError(
                f"no feasible baseline task for {family}: {observed}"
            )
    return tuple(selected)


def _validate_selection(selection: tuple[PositiveSelection, ...]) -> None:
    if tuple(item.family for item in selection) != FAMILIES:
        raise ValueError("selection must contain one task per family in declared order")
    for item in selection:
        if item.case_id != CANDIDATES[item.family][item.round]:
            raise ValueError("selection does not follow the frozen fallback order")
        if not BASELINE_MIN <= item.successes <= BASELINE_MAX:
            raise ValueError("selected task is outside the feasibility window")


def make_train_rows(
    selected: tuple[str, ...], cases: dict[str, LearningCase]
) -> tuple[PositiveRow, ...]:
    if len(selected) != len(FAMILIES) or len(set(selected)) != len(FAMILIES):
        raise ValueError("exactly three distinct selected tasks required")
    if tuple(_case_family(cases[case_id]) for case_id in selected) != FAMILIES:
        raise ValueError("selected tasks must be ordered by family")
    rng = random.Random(SHUFFLE_SEED)
    rows = []
    group = 0
    for _ in range(EPOCHS):
        order = list(selected)
        rng.shuffle(order)
        for case_id in order:
            for repeat in range(GROUP_SIZE):
                rows.append(
                    PositiveRow(
                        run_id=f"pc1_train_{group:02}_{repeat}",
                        phase="train",
                        family=_case_family(cases[case_id]),
                        case_id=case_id,
                        seed=TRAIN_SEED_BASE + group * 100 + repeat,
                        repeat=repeat,
                        group=f"train_{group:02}",
                        checkpoint=group,
                    )
                )
            group += 1
    return tuple(rows)


def make_eval_rows(
    selected: tuple[str, ...], cases: dict[str, LearningCase]
) -> tuple[PositiveRow, ...]:
    rows = []
    for checkpoint in CHECKPOINTS[1:]:
        for task_index, case_id in enumerate(selected):
            for repeat in range(EVAL_SEEDS):
                rows.append(
                    PositiveRow(
                        run_id=f"pc1_eval_{checkpoint:02}_{task_index}_{repeat}",
                        phase="eval",
                        family=_case_family(cases[case_id]),
                        case_id=case_id,
                        seed=EVAL_SEED_BASE + task_index * 100 + repeat,
                        repeat=repeat,
                        group=f"eval_{checkpoint:02}",
                        checkpoint=checkpoint,
                    )
                )
    return tuple(rows)


def make_plan(
    selection: tuple[PositiveSelection, ...],
    *,
    selection_evidence_sha256: str | None = None,
) -> PositiveControlPlan:
    """Freeze the deterministic queue after the pre-declared baseline gate."""
    _validate_selection(selection)
    cases = positive_cases()
    selected = tuple(item.case_id for item in selection)
    baseline_rows = make_baseline_rows()
    train_rows = make_train_rows(selected, cases)
    eval_rows = make_eval_rows(selected, cases)
    rows = len(baseline_rows) + len(train_rows) + len(eval_rows)
    groups = len({row.group for row in train_rows})
    required_cny = (
        rows * RUN_COST + groups * (GROUP_TRAIN_COST + GROUP_CLOSE_COST) + INITIAL_COST + EXTRA_COST
    )
    case_sha256 = _candidate_case_sha256(cases)
    plan = PositiveControlPlan(
        families=FAMILIES,
        selected=selected,
        selection=selection,
        baseline_rows=baseline_rows,
        train_rows=train_rows,
        eval_rows=eval_rows,
        checkpoints=CHECKPOINTS,
        required_cny=required_cny,
        case_sha256=case_sha256,
        selection_evidence_sha256=selection_evidence_sha256,
    )
    verify_plan(plan)
    return plan


def verify_plan(plan: PositiveControlPlan) -> None:  # noqa: PLR0912 - frozen invariants
    cases = positive_cases()
    if plan.families != FAMILIES or plan.checkpoints != CHECKPOINTS:
        raise ValueError("plan constants changed")
    _validate_selection(plan.selection)
    if plan.selected != tuple(item.case_id for item in plan.selection):
        raise ValueError("selected tasks do not match selection")
    if len(plan.baseline_rows) != len(FAMILIES) * BASELINE_ROUNDS * EVAL_SEEDS:
        raise ValueError("baseline denominator changed")
    expected_baseline = {
        case_id for family in FAMILIES for case_id in CANDIDATES[family][:BASELINE_ROUNDS]
    }
    if {row.case_id for row in plan.baseline_rows} != expected_baseline:
        raise ValueError("baseline candidate set changed")
    if len(plan.train_rows) != EPOCHS * len(FAMILIES) * GROUP_SIZE:
        raise ValueError("training denominator changed")
    if len(plan.eval_rows) != (len(CHECKPOINTS) - 1) * len(FAMILIES) * EVAL_SEEDS:
        raise ValueError("evaluation denominator changed")
    groups = {row.group for row in plan.train_rows}
    if len(groups) != EPOCHS * len(FAMILIES):
        raise ValueError("training group count changed")
    if any(
        sum(1 for row in plan.train_rows if row.group == group) != GROUP_SIZE for group in groups
    ):
        raise ValueError("training group size changed")
    for epoch in range(EPOCHS):
        epoch_rows = [
            row
            for row in plan.train_rows
            if epoch * len(FAMILIES) <= row.checkpoint < (epoch + 1) * len(FAMILIES)
        ]
        if {row.case_id for row in epoch_rows} != set(plan.selected):
            raise ValueError("each epoch must contain every selected task once")
    if {row.case_id for row in plan.train_rows + plan.eval_rows} != set(plan.selected):
        raise ValueError("train/eval rows use an undeclared task")
    if any(_case_family(cases[row.case_id]) != row.family for row in plan.eval_rows):
        raise ValueError("evaluation family binding changed")
    train_seeds = {row.seed for row in plan.train_rows}
    held_out = {row.seed for row in plan.baseline_rows + plan.eval_rows}
    if train_seeds & held_out:
        raise ValueError("training and held-out seeds overlap")
    for checkpoint in plan.checkpoints[1:]:
        for task_index, case_id in enumerate(plan.selected):
            expected = {
                plan.eval_seed_base + task_index * 100 + repeat for repeat in range(EVAL_SEEDS)
            }
            observed = {
                row.seed
                for row in plan.eval_rows
                if row.case_id == case_id and row.checkpoint == checkpoint
            }
            if observed != expected:
                raise ValueError("evaluation seeds are not paired across checkpoints")
            baseline = {row.seed for row in plan.baseline_rows if row.case_id == case_id}
            if baseline != expected:
                raise ValueError("baseline seeds do not match evaluation seeds")
    for family in FAMILIES:
        for round_index in range(BASELINE_ROUNDS):
            group = f"base_{FAMILIES.index(family)}_{round_index}"
            if sum(1 for row in plan.baseline_rows if row.group == group) != EVAL_SEEDS:
                raise ValueError("baseline group size changed")


def evidence_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_plan(path: Path) -> PositiveControlPlan:
    plan = PositiveControlPlan.model_validate_json(path.read_bytes())
    expected = make_plan(plan.selection, selection_evidence_sha256=plan.selection_evidence_sha256)
    if plan != expected:
        raise ValueError("positive-control plan changed")
    return plan

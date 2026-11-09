"""Versioned single-seed attempted-slot queue and complete resource allocation."""

import random

# ruff: noqa: PERF401 - explicit loops follow the frozen scientific queue order
from decimal import Decimal
from typing import Literal

from pydantic import Field

from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_rollout import _case_digest
from minidsh_rl.learning_schedule import (
    GROUP_CLOSE_COST,
    GROUP_TRAIN_COST,
    INITIAL_COST,
    RUN_COST,
    FrozenRecord,
)

CHECKPOINTS = (0, 8, 16, 24)
TRAIN_SLOTS = 24
READY_GROUPS = 6
GROUP_SIZE = 4
MIN_MIXED = 3
MIN_GROUPS = 2
MIN_SUCCESS = 6
MAX_SUCCESS = 18
EXTRA = Decimal("2.04")  # audits/setup + two .01 known restores + contingency


class CurveRow(FrozenRecord):
    run_id: str
    phase: Literal["dev", "train", "validation"]
    case_id: str
    seed: int = Field(ge=0)
    group: str
    checkpoint: int = Field(ge=0, le=TRAIN_SLOTS)


class CurvePlan(FrozenRecord):
    schema_version: Literal[1] = 1
    dataset_version: Literal["curve_dataset_v2", "curve_dataset_v4"] = "curve_dataset_v2"
    profile: Literal["qwen35_4b_r16_seed17_tool_example_success_v1"] = (
        "qwen35_4b_r16_seed17_tool_example_success_v1"
    )
    rows: tuple[CurveRow, ...]
    case_sha256: dict[str, str]
    required_cny: Decimal
    sources: dict[str, str] = Field(default_factory=dict)
    data_summary_sha256: str = ""
    config_sha256: str = ""
    readiness_sha256: str | None = None
    finance_directory: str | None = None
    budget_baseline: Decimal | None = None
    initial_inputs: dict[str, str] = Field(default_factory=dict)
    recovery_sha256: str | None = None
    initialization: Literal["restore_optimizer", "verified_fresh_seed17"] = "restore_optimizer"
    source_optimizer_archive_sha256: str | None = None


def curve_cases_for(
    dataset: Literal["curve_dataset_v2", "curve_dataset_v4"],
) -> tuple[LearningCase, ...]:
    return curve_cases_v2() if dataset == "curve_dataset_v2" else curve_cases_v4()


def make_curve_plan(
    *, dataset: Literal["curve_dataset_v2", "curve_dataset_v4"] = "curve_dataset_v2"
) -> CurvePlan:
    """All 216 rows fixed before any sampling; validation cannot select checkpoint."""
    cases = curve_cases_for(dataset)
    prefix = "curve2" if dataset == "curve_dataset_v2" else "curve4"
    rows = []
    for i, case in enumerate(c for c in cases if c.split == "dev"):
        for repeat in range(GROUP_SIZE):
            rows.append(
                CurveRow(
                    run_id=f"{prefix}_dev_{i:02}_{repeat}",
                    phase="dev",
                    case_id=case.task.case_id,
                    seed=926000 + i * 1000 + repeat * 20,
                    group=f"dev_{i:02}",
                    checkpoint=0,
                )
            )
    train = [c for c in cases if c.split == "train"]
    order = []
    rng = random.Random(17)
    for _ in range(2):
        epoch = list(train)
        rng.shuffle(epoch)
        order.extend(epoch)
    for slot in range(TRAIN_SLOTS + 1):
        if slot:
            case = order[slot - 1]
            for repeat in range(GROUP_SIZE):
                rows.append(
                    CurveRow(
                        run_id=f"{prefix}_train_{slot:02}_{repeat}",
                        phase="train",
                        case_id=case.task.case_id,
                        seed=17 + slot * 1000 + repeat * 20,
                        group=f"train_{slot:03}",
                        checkpoint=slot,
                    )
                )
        if slot in CHECKPOINTS:
            validation = [c for c in cases if c.split == "validation"]
            # Rotate task order across checkpoints to reduce fixed order effects.
            shift = CHECKPOINTS.index(slot) * 3
            for i in [*range(shift, len(validation)), *range(shift)]:
                for repeat in range(2):
                    rows.append(
                        CurveRow(
                            run_id=f"{prefix}_val_{slot:02}_{i:02}_{repeat}",
                            phase="validation",
                            case_id=validation[i].task.case_id,
                            seed=(936000 if dataset == "curve_dataset_v2" else 946000)
                            + i * 1000
                            + repeat * 20,
                            group=f"val_{i:02}_{repeat}",
                            checkpoint=slot,
                        )
                    )
    return CurvePlan(
        dataset_version=dataset,
        rows=tuple(rows),
        case_sha256={c.task.case_id: _case_digest(c) for c in cases},
        required_cny=len(rows) * RUN_COST
        + TRAIN_SLOTS * (GROUP_TRAIN_COST + GROUP_CLOSE_COST)
        + INITIAL_COST
        + EXTRA,
    )


def readiness_gate(rewards: list[list[bool]], families: list[str], feedback: set[str]) -> bool:
    """All declared dev groups, not a successful subset, determine readiness."""
    if len(rewards) != READY_GROUPS or len(families) != READY_GROUPS:
        return False
    if any(
        len(group) != GROUP_SIZE or any(type(r) is not bool for r in group) for group in rewards
    ):
        return False
    mixed_families = {
        family for family, group in zip(families, rewards, strict=True) if len(set(group)) > 1
    }
    return (
        sum(len(set(group)) > 1 for group in rewards) >= MIN_MIXED
        and len(mixed_families) >= MIN_GROUPS
        and MIN_SUCCESS <= sum(sum(group) for group in rewards) <= MAX_SUCCESS
        and len(feedback & set(families)) >= MIN_GROUPS
    )

"""Fixture execution of the positive-control state machine; no model calls."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl.contracts import Turn
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_rollout import LearningGroup, LearningRollout, PolicyBinding
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.positive_control import PositiveRow, load_plan
from minidsh_rl.positive_control_run import execute_positive_control, prepare_positive_control

_BASELINE = {
    ("match_rank", 0): 3,
    ("match_rank", 1): 2,
    ("match_rank", 2): 0,
    ("dependency_batches", 0): 4,
    ("dependency_batches", 1): 3,
    ("dependency_batches", 2): 0,
    ("table_join", 0): 2,
    ("table_join", 1): 1,
    ("table_join", 2): 0,
}


def _checkpoint(index: int) -> Checkpoint:
    return Checkpoint(
        execution_kind="contract_fixture",
        sampler_uri=f"contract://sampler/{index}",
        optimizer_uri=f"contract://optimizer/{index}",
        policy=PolicyBinding(
            kind="provider",
            policy_id=f"fixture_{index}",
            snapshot_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
            tokenizer_sha256="b" * 64,
        ),
    )


class FixtureRuntime:
    execution_kind: Literal["contract_fixture"] = "contract_fixture"

    def __init__(self, mode: Literal["pass", "fail", "zero"]) -> None:
        self.mode = mode
        self.updates = 0
        self.samples = 0

    async def initialize(self, directory: Path) -> Checkpoint:
        value = _checkpoint(0)
        atomic_record(directory / "initial.json", value, create=True)
        return value

    async def sample(
        self,
        case: LearningCase,
        row: PositiveRow,
        before: Checkpoint,
        output: Path,
    ) -> LearningRollout:
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
        if row.phase == "baseline":
            success = row.repeat < _BASELINE[(row.family, row.round or 0)]
        elif row.phase == "train":
            success = self.mode != "zero" and (row.checkpoint + row.repeat) % 2 == 0
        else:
            success = self.mode == "pass" and row.checkpoint == 18 and row.repeat < 7
        await asyncio.to_thread(
            (output / "fixture.json").write_text,
            json.dumps(
                {
                    "run_id": row.run_id,
                    "seed": row.seed,
                    "success": success,
                    "policy": before.policy.policy_id,
                    "kind": "contract_fixture",
                }
            ),
        )
        self.samples += 1
        return LearningRollout(
            case,
            row.run_id,
            row.seed,
            before.policy,
            success,
            "finished",
            (Turn((1,), (2,), (-0.1,)),),
            2,
            "c" * 64,
            verification_executed=True,
            local_failed_calls=0,
        )

    async def update(
        self,
        key: str,
        group: LearningGroup,
        before: Checkpoint,
        output: Path,
    ) -> Checkpoint:
        has_signal = group.batch is not None and group.batch.has_signal
        await asyncio.to_thread(
            (output / "receipt.json").write_text,
            json.dumps({"key": key, "updated": has_signal, "run_ids": group.run_ids}),
        )
        if not has_signal:
            return before
        self.updates += 1
        return _checkpoint(self.updates)


async def test_pass_fixture_reaches_pass(tmp_path: Path) -> None:
    root = tmp_path / "pass"
    prepare_positive_control(root)
    state = await execute_positive_control(root, FixtureRuntime("pass"))
    assert state.phase == "finished"
    report = json.loads((root / "final-report.json").read_text())
    assert report["status"] == "pass"
    assert report["baseline_successes"] == 9
    assert report["final_successes"] == 21
    assert report["delta"] == 12
    assert report["updates"] == 18
    assert report["completed_rows"] == 216
    assert {item["final"] for item in report["per_task"].values()} == {7}
    plan = load_plan(root / "plan.json")
    assert plan.selection_evidence_sha256 == digest(root / "baseline-results.json")
    assert state.selected == list(plan.selected)


async def test_fail_fixture_reaches_fail(tmp_path: Path) -> None:
    root = tmp_path / "fail"
    prepare_positive_control(root)
    state = await execute_positive_control(root, FixtureRuntime("fail"))
    assert state.phase == "finished"
    report = json.loads((root / "final-report.json").read_text())
    assert report["status"] == "fail"
    assert report["baseline_successes"] == 9
    assert report["final_successes"] == 0
    assert report["updates"] == 18


async def test_zero_signal_stops_inconclusive(tmp_path: Path) -> None:
    root = tmp_path / "zero"
    prepare_positive_control(root)
    state = await execute_positive_control(root, FixtureRuntime("zero"))
    assert state.phase == "stopped"
    assert len(state.groups) == 10
    assert len(state.updated) == 0
    report = json.loads((root / "final-report.json").read_text())
    assert report["status"] == "inconclusive"
    assert report["final_measured"] is False


def test_prepare_refuses_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    prepare_positive_control(root)
    with pytest.raises(ValueError, match="already exists"):
        prepare_positive_control(root)

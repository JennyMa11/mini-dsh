"""Frozen same-scale Base/GSPO study and its single-writer execution contract."""

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.contracts import advantages
from minidsh_rl.curve_artifacts import tree_hashes, verify_hashes
from minidsh_rl.curve_execution import CurveState, accept_update
from minidsh_rl.evaluate_v2 import evaluation_queue
from minidsh_rl.gspo import GSPOBatch, GSPOConfig, build_gspo_batch
from minidsh_rl.learning_execution import Checkpoint, GroupRecord, ResourcePort
from minidsh_rl.learning_gates import _sources
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest
from minidsh_rl.rollout_v2 import Rollout
from minidsh_rl.tasks_v2 import expansion_tasks, public_files, verifier_source
from minidsh_rl.train_v2 import training_schedule

MAX_RESUMES = 2
ZERO_SIGNAL_LIMIT = 10
GROUPS = 40
G = 4
RUN_UPPER = Decimal(".06")
GROUP_UPPER = Decimal("1.65")
CLOSING = Decimal(".30")
CONFIG = GSPOConfig(0.2, 0.2)


class StudyRow(FrozenRecord):
    run_id: str
    phase: Literal["train", "validation", "test"]
    case_id: str
    seed: int
    slot: int
    policy: Literal["train", "base", "gspo"]


class StudyPlan(FrozenRecord):
    version: Literal["gspo_expansion_efficacy_v1"] = "gspo_expansion_efficacy_v1"
    comparison: Literal["base_vs_gspo_only"] = "base_vs_gspo_only"
    test_previously_opened: Literal[True] = True
    rows: tuple[StudyRow, ...]
    cases: dict[str, str]
    sources: dict[str, str] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    finance: str = ""
    image: str = ""
    tokenizer_sha256: str = ""


def make_study() -> StudyPlan:
    rows = []
    for slot, task in enumerate(training_schedule("pilot")):
        for repeat in range(G):
            rows.append(  # noqa: PERF401 - explicit frozen queue
                StudyRow(
                    run_id=f"gspo40_train_{slot:03}_{repeat}",
                    phase="train",
                    case_id=task.case_id,
                    seed=17 + slot * 100 + repeat,
                    slot=slot,
                    policy="train",
                )
            )
    for phase in ("validation", "test"):
        for index, row in enumerate(evaluation_queue(phase)):
            policy: Literal["base", "gspo"] = "base" if row["policy"] == "base" else "gspo"
            rows.append(
                StudyRow(
                    run_id=f"gspo40_{phase}_{index:03}_{policy}",
                    phase=phase,
                    case_id=str(row["case_id"]),
                    seed=int(str(row["seed"])),
                    slot=GROUPS,
                    policy=policy,
                )
            )
    return StudyPlan(
        rows=tuple(rows),
        cases={
            task.case_id: hashlib.sha256(
                json.dumps(
                    {
                        "public": public_files(task),
                        "reference": task.reference,
                        "verifier": verifier_source(task, "frozen_study_marker"),
                        "split": task.split,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for task in expansion_tasks()
        },
    )


def batch_digest(batch: GSPOBatch) -> str:
    value = {
        "datums": [d.model_dump(mode="json") for d in batch.datums],
        "trajectories": [asdict(t) for t in batch.trajectories],
        "config": asdict(CONFIG),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def evaluation_remaining(plan: StudyPlan, state: CurveState) -> Decimal:
    return (
        sum(r.phase != "train" and r.run_id not in state.completed for r in plan.rows) * RUN_UPPER
        + CLOSING
    )


class StudyRuntime(Protocol):
    execution_kind: Literal["provider", "contract_fixture"]

    async def initialize(self, root: Path) -> Checkpoint: ...
    async def reconnect(self, before: Checkpoint, attempt: int) -> None: ...
    async def sample(
        self, row: StudyRow, before: Checkpoint, output: Path, reserve: Decimal
    ) -> Rollout: ...
    async def update(
        self, key: str, batch: GSPOBatch, before: Checkpoint, output: Path, reserve: Decimal
    ) -> None: ...


def checked_sample(row: StudyRow, before: Checkpoint, folder: Path, value: Rollout) -> None:
    raw = json.loads((folder / "result.json").read_text())
    if (
        value.run_id != row.run_id
        or raw["run_id"] != row.run_id
        or raw["case_id"] != row.case_id
        or raw["seed"] != row.seed
        or raw["policy_id"] != before.policy.policy_id
        or raw["criterion_version"] != "expansion_v2"
        or raw["success"] is not value.success
        or raw["status"] != value.status
        or value.status == "cancelled"
        or not value.turns
    ):
        raise ValueError("rollout identity or complete raw data mismatch")


async def execute_study(  # noqa: PLR0912, PLR0915 - explicit leased transitions
    root: Path,
    plan: StudyPlan,
    budget: ResourcePort,
    runtime: StudyRuntime,
    *,
    resume: bool = False,
) -> CurveState:
    lease = FileLease(root / "controller.lock")
    try:
        if StudyPlan.model_validate_json((root / "plan.json").read_bytes()) != plan:
            raise ValueError("frozen study changed")
        path = root / "state.json"
        existed = path.exists()
        state = (
            CurveState.model_validate_json(path.read_bytes())
            if existed
            else CurveState(plan_sha256=digest(root / "plan.json"), kind=runtime.execution_kind)
        )
        if state.kind != runtime.execution_kind or state.plan_sha256 != digest(root / "plan.json"):
            raise ValueError("controller provenance changed")
        verify_hashes(root, state.artifacts)
        if state.phase != "running":
            return state
        if existed and not resume:
            raise ValueError("explicit known-boundary resume required")
        if budget.has_pending:
            raise ValueError("unknown current request blocks execution")
        if state.pending is not None:
            if state.pending == "update":
                accept_update(root, state, state.key)
            else:
                raise ValueError("unknown in-flight operation; do not replay")

        def save() -> None:
            atomic_record(path, state)

        def invariant() -> None:
            if digest(root / "plan.json") != state.plan_sha256 or (
                state.kind == "provider" and _sources() != plan.sources
            ):
                raise ValueError("source or plan changed during execution")
            if budget.has_pending:
                raise ValueError("unknown current provider request")

        if state.initial is None:
            invariant()
            if budget.available < GROUP_UPPER + evaluation_remaining(plan, state) + Decimal(".25"):
                raise ValueError("complete first group and paired evaluation not funded")
            state.pending = "initialize"
            save()
            state.initial = state.current = await runtime.initialize(root)
            if state.initial.execution_kind != state.kind:
                raise ValueError("initial provenance mismatch")
            state.artifacts.update(tree_hashes(root, root / "initial-sdk"))
            state.artifacts.update(tree_hashes(root, root / "policies"))
            state.artifacts["initial.json"] = digest(root / "initial.json")
            state.pending = None
            save()
        else:
            if state.current is None or state.resumes >= MAX_RESUMES:
                raise ValueError("resume allowance exhausted")
            state.resumes += 1
            state.pending = "resume"
            save()
            await runtime.reconnect(state.current, state.resumes)
            state.pending = None
            save()

        async def sample(row: StudyRow, before: Checkpoint, reserve: Decimal) -> Rollout:
            invariant()
            folder = root / "rollouts" / row.run_id
            state.pending = "sample"
            state.key = row.run_id
            save()
            result = await runtime.sample(row, before, folder, reserve)
            checked_sample(row, before, folder, result)
            invariant()
            state.completed.append(row.run_id)
            state.artifacts.update(tree_hashes(root, folder))
            state.pending = None
            state.key = ""
            save()
            return result

        for slot in range(GROUPS):
            key = f"train_{slot:03}"
            if key in state.groups:
                continue
            rows = [r for r in plan.rows if r.phase == "train" and r.slot == slot]
            if any(r.run_id in state.completed for r in rows):
                raise ValueError("partial G4 cannot be replayed")
            reserve = evaluation_remaining(plan, state)
            if budget.available < GROUP_UPPER + reserve:
                state.phase = "stopped"
                save()
                return state
            if state.current is None:
                raise ValueError("current policy missing")
            before = state.current
            values = [await sample(row, before, reserve) for row in rows]
            rewards = tuple(float(v.success) for v in values)
            batch = build_gspo_batch(tuple(v.turns for v in values), advantages(rewards))
            folder = root / "updates" / key
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "rewards.json").write_text(
                json.dumps(
                    {
                        "rewards": rewards,
                        "advantages": advantages(rewards),
                        "case_id": rows[0].case_id,
                    }
                )
            )
            record = GroupRecord(
                before=before,
                run_ids=tuple(v.run_id for v in values),
                datum_sha256=batch_digest(batch),
                has_signal=batch.has_signal,
                input_sha256={
                    p: h for p, h in state.artifacts.items() if any(r.run_id in p for r in rows)
                },
            )
            atomic_record(folder / "group.json", record, create=True)
            state.pending = "update"
            state.key = key
            save()
            invariant()
            await runtime.update(key, batch, before, folder, reserve)
            accept_update(root, state, key)
            save()
            print_progress(
                {
                    "phase": "train",
                    "groups": len(state.groups),
                    "completed": len(state.completed),
                    "updates": len(state.updated),
                    "successes": sum(rewards),
                }
            )
            if state.zero_streak >= ZERO_SIGNAL_LIMIT:
                state.phase = "zero_signal_stop"
                save()
                return state
        # Test admission depends on complete training/audit, never validation direction.
        for row in (r for r in plan.rows if r.phase != "train"):
            if row.run_id in state.completed:
                continue
            invariant()
            if budget.available < evaluation_remaining(plan, state):
                state.phase = "stopped"
                save()
                return state
            before = state.initial if row.policy == "base" else state.current
            if before is None:
                raise ValueError("paired policy missing")
            value = await sample(row, before, evaluation_remaining(plan, state) - RUN_UPPER)
            print_progress(
                {
                    "phase": row.phase,
                    "completed": len(state.completed),
                    "declared": len(plan.rows),
                    "success": value.success,
                    "policy": row.policy,
                }
            )
        state.phase = "finished"
        save()
        return state
    finally:
        lease.close()


def print_progress(value: dict[str, object]) -> None:
    import sys  # noqa: PLC0415 - no provider logging

    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()

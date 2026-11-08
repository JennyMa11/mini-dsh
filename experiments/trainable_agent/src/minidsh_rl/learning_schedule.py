"""Frozen full-study queues and conservative resources; no model client construction."""

import hashlib
import json
import os
import random
import tarfile
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from minidsh_rl.learning_gates import PROJECT, _sources
from minidsh_rl.learning_rollout import Digest, _case_digest
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.reprice import milli_upper

ExecutionKind = Literal["provider", "contract_fixture"]
FundingPolicy = Literal["full_remaining_v1", "confirmed_stage_v1"]
RUN_COST = Decimal(".09")
GROUP_TRAIN_COST = milli_upper(0, 0, 4 * 12 * (6144 - 1))
GROUP_CLOSE_COST = Decimal(".21")  # optim .01 + optimizer state .10 + sampler .10
INITIAL_COST = Decimal(".21")  # client .01 + optimizer state .10 + sampler .10
EXTRA_COST = Decimal(".50")
RESUME_COST = Decimal(".01")
MAX_RESUMES = 2
STAGED_MAX_RESUMES = 12
TRAIN_GROUPS = 8
GROUP_SIZE = 4


class FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class QueueRow(FrozenRecord):
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    phase: Literal["readiness", "train", "test"]
    case_id: str
    seed: int = Field(ge=0, strict=True)
    group: str
    policy: Literal["current", "base", "rl"]


class LearningPlan(FrozenRecord):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    execution_kind: ExecutionKind
    funding_policy: FundingPolicy = "full_remaining_v1"
    dataset_version: Literal["learnability_dataset_v2"] = "learnability_dataset_v2"
    rows: tuple[QueueRow, ...]
    case_sha256: dict[str, Digest]
    required_cny: Decimal
    sources: dict[str, Digest] = Field(default_factory=dict)
    data_evidence: str | None = None
    data_evidence_sha256: Digest | None = None
    runtime_config_sha256: Digest | None = None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def complete_allocation() -> Decimal:
    """All 80 rollouts, eight nonzero updates, confirmed saves and extra allowance."""
    return (
        80 * RUN_COST
        + TRAIN_GROUPS * (GROUP_TRAIN_COST + GROUP_CLOSE_COST)
        + INITIAL_COST
        + EXTRA_COST
    )


def resume_limit(plan: LearningPlan) -> int:
    """Different funding policies share the same finite extra-cost allocation."""
    return STAGED_MAX_RESUMES if plan.funding_policy == "confirmed_stage_v1" else MAX_RESUMES


def atomic_record(path: Path, record: BaseModel, *, create: bool = False) -> None:
    """One leased writer; publish complete 0600 records before the next side effect."""
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(record.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            if create:
                os.link(temporary, path)
            else:
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def make_plan(
    kind: ExecutionKind,
    *,
    experiment_id: str | None = None,
    funding_policy: FundingPolicy = "full_remaining_v1",
) -> LearningPlan:
    """Fixed case order/seeds; experiment ID only separates artifacts, never sampling."""
    name = experiment_id or "learn_" + uuid4().hex[:12]
    cases = learning_cases()
    train = [c for c in cases if c.split == "train"]
    test = [c for c in cases if c.split == "test"]
    rows: list[QueueRow] = []
    for index, case in enumerate(train):
        if case.variant != 0:
            continue
        group = f"ready_{index:03}"
        rows.extend(
            QueueRow(
                run_id=f"{name}_{group}_{repeat}",
                phase="readiness",
                case_id=case.task.case_id,
                seed=9000 + index * 100 + repeat,
                group=group,
                policy="base",
            )
            for repeat in range(GROUP_SIZE)
        )
    order = list(train)
    random.Random(17).shuffle(order)
    for slot, case in enumerate(order):
        group = f"train_{slot:03}"
        rows.extend(
            QueueRow(
                run_id=f"{name}_{group}_{repeat}",
                phase="train",
                case_id=case.task.case_id,
                seed=17 + slot * 100 + repeat,
                group=group,
                policy="current",
            )
            for repeat in range(GROUP_SIZE)
        )
    for index, case in enumerate(test):
        for repeat in range(2):
            pair = index * 2 + repeat
            policies: tuple[Literal["base", "rl"], ...] = (
                ("base", "rl") if pair % 2 == 0 else ("rl", "base")
            )
            rows.extend(
                QueueRow(
                    run_id=f"{name}_test_{pair:03}_{policy}",
                    phase="test",
                    case_id=case.task.case_id,
                    seed=54017 + pair * 97,
                    group=f"pair_{pair:03}",
                    policy=policy,
                )
                for policy in policies
            )
    return LearningPlan(
        experiment_id=name,
        execution_kind=kind,
        funding_policy=funding_policy,
        rows=tuple(rows),
        case_sha256={c.task.case_id: _case_digest(c) for c in cases},
        required_cny=complete_allocation(),
    )


def prepare(
    output: Path,
    *,
    kind: ExecutionKind = "provider",
    data_evidence: Path | None = None,
    runtime_config: BaseModel | None = None,
    funding_policy: FundingPolicy = "full_remaining_v1",
) -> LearningPlan:
    """Freeze the entire queue locally; this function never opens a service session."""
    plan = make_plan(kind, funding_policy=funding_policy)
    sources = _sources()
    if data_evidence is not None:
        proof = json.loads(data_evidence.read_text())
        if proof.get("targeted_feedback_coverage_passed") is not True:
            raise ValueError("new data coverage evidence missing")
        bindings = proof["case_bindings"]
        for case in learning_cases():
            old = bindings[case.task.case_id]
            if (
                old["dataset_version"] != case.dataset_version
                or old["split"] != case.split
                or old["family_group"] != case.family_group
                or old["criterion_version"] != case.criterion_version
                or old["public_feedback_version"] != case.public_feedback_version
                or old["reference_sha256"]
                != hashlib.sha256(
                    json.dumps(dict(case.task.reference), sort_keys=True).encode()
                ).hexdigest()
                or old["public_checker_sha256"]
                != hashlib.sha256(dict(case.task.files)["public_tests.py"].encode()).hexdigest()
                or old["public_fixture_sha256"]
                != hashlib.sha256(
                    json.dumps(dict(case.task.files), sort_keys=True).encode()
                ).hexdigest()
                or old["private_checker_sha256"]
                != hashlib.sha256(case.task.hidden_checks.encode()).hexdigest()
            ):
                raise ValueError("data evidence does not bind current cases")
    plan = plan.model_copy(
        update={
            "sources": sources,
            "data_evidence": str(data_evidence) if data_evidence is not None else None,
            "data_evidence_sha256": digest(data_evidence) if data_evidence is not None else None,
        }
    )
    output.mkdir(parents=True, exist_ok=False)
    if runtime_config is not None:
        atomic_record(output / "execution-config.json", runtime_config, create=True)
        plan = plan.model_copy(
            update={"runtime_config_sha256": digest(output / "execution-config.json")}
        )
    atomic_record(output / "plan.json", plan, create=True)
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for path in sources:
            archive.add(path, arcname=str(Path(path).relative_to(PROJECT)), recursive=False)
    return plan


def load_plan(directory: Path) -> LearningPlan:
    plan = LearningPlan.model_validate_json((directory / "plan.json").read_bytes())
    expected = make_plan(plan.execution_kind, experiment_id=plan.experiment_id)
    if (
        plan.rows != expected.rows
        or plan.case_sha256 != expected.case_sha256
        or plan.required_cny != complete_allocation()
    ):
        raise ValueError("predeclared plan changed")
    if not plan.sources or plan.sources != _sources():
        raise ValueError("frozen execution source changed")
    if (
        plan.runtime_config_sha256 is not None
        and digest(directory / "execution-config.json") != plan.runtime_config_sha256
    ):
        raise ValueError("frozen execution config changed")
    if (
        plan.data_evidence is not None
        and digest(Path(plan.data_evidence)) != plan.data_evidence_sha256
    ):
        raise ValueError("data evidence changed")
    return plan

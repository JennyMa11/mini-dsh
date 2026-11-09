"""Read-only reuse of accepted v3 evidence, never synthetic readiness or rescoring."""

import hashlib
import tarfile
from pathlib import Path
from typing import Literal

from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.curve_schedule import readiness_gate
from minidsh_rl.development_cases import development_cases, public_checker
from minidsh_rl.development_execution import DevelopmentState
from minidsh_rl.development_schedule import DevelopmentPlan, make_development_plan
from minidsh_rl.learning_feedback import FeedbackProof, _artifacts, _failed, feedback_indices
from minidsh_rl.learning_gates import PROJECT
from minidsh_rl.learning_rollout import load_rollout
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest


class ReadinessEvidence(FrozenRecord):
    schema_version: Literal[1] = 1
    source_root: str
    inputs: dict[str, str]
    successes: int
    mixed_groups: int
    mixed_families: tuple[str, ...]
    feedback_families: tuple[str, ...]
    new_provider_calls: Literal[0] = 0


def inspect_readiness(root: Path) -> ReadinessEvidence:  # noqa: PLR0912 - independent historical evidence audit
    plan_path, state_path = root / "plan.json", root / "state.json"
    plan = DevelopmentPlan.model_validate_json(plan_path.read_bytes())
    state = DevelopmentState.model_validate_json(state_path.read_bytes())
    expected = make_development_plan()
    if (
        plan.kind != "provider"
        or plan.rows != expected.rows
        or plan.case_sha256 != expected.case_sha256
        or state.phase != "accepted"
        or state.pending is not None
        or state.policy.kind != "provider"
        or state.plan_sha256 != digest(plan_path)
        or set(state.completed) != {r.run_id for r in plan.rows}
    ):
        raise ValueError("complete accepted provider readiness required")

    verify_hashes(root, state.artifacts)
    inputs = dict(plan.inputs)
    inputs.update({str((root / name).resolve()): value for name, value in state.artifacts.items()})
    inputs.update(
        {str(p.resolve()): digest(p) for p in (plan_path, state_path, root / "source.tar.gz")}
    )
    for path, expected_hash in inputs.items():
        if Path(path).is_symlink() or digest(Path(path)) != expected_hash:
            raise ValueError("historical readiness input changed")

    with tarfile.open(root / "source.tar.gz") as archive:
        for name, expected_hash in plan.sources.items():
            stream = archive.extractfile(str(Path(name).relative_to(PROJECT)))
            if stream is None or hashlib.sha256(stream.read()).hexdigest() != expected_hash:
                raise ValueError("historical execution archive changed")
    cases = {c.task.case_id: c for c in development_cases()}
    rewards: dict[str, list[bool]] = {}
    families: dict[str, str] = {}
    recovered = set()
    for row in plan.rows:
        case = cases[row.case_id]
        if dict(case.task.files)["public_tests.py"] != public_checker(case.task.family):
            raise ValueError("readiness feedback profile changed")
        folder = root / "rollouts" / row.run_id
        value = load_rollout(case, folder, state.policy)
        if value.seed != row.seed or value.run_id != row.run_id or value.budget_violations:
            raise ValueError("readiness trajectory binding mismatch")
        rewards.setdefault(row.group, []).append(value.success)
        families[row.group] = case.family_group
        if not value.success:
            continue
        proof_dir = root / "feedback" / row.run_id
        proof = FeedbackProof.model_validate_json((proof_dir / "feedback.json").read_bytes())
        if (
            proof.run_id != row.run_id
            or proof.case_id != case.task.case_id
            or proof.criterion_version != case.criterion_version
            or proof.source_kind != "provider"
            or proof.artifacts != _artifacts(proof_dir)
            or proof.checked_patch_indices != feedback_indices(folder, row.run_id)
            or proof.failed_private_patch_indices != _failed(proof.checks)
            or proof.recovered != bool(_failed(proof.checks))
        ):
            raise ValueError("readiness feedback proof changed")
        for name, value_hash in proof.inputs.items():
            if digest(folder / name) != value_hash:
                raise ValueError("readiness feedback source changed")
        if proof.recovered:
            recovered.add(case.family_group)
    if (
        state.rewards != rewards
        or state.families != families
        or set(state.recovered_families) != recovered
        or not readiness_gate(list(rewards.values()), list(families.values()), recovered)
    ):
        raise ValueError("original readiness threshold not satisfied")
    mixed = [key for key, group in rewards.items() if len(set(group)) > 1]
    return ReadinessEvidence(
        source_root=str(root.resolve()),
        inputs=inputs,
        successes=sum(sum(group) for group in rewards.values()),
        mixed_groups=len(mixed),
        mixed_families=tuple(sorted({families[key] for key in mixed})),
        feedback_families=tuple(sorted(recovered)),
    )


def freeze_readiness(source: Path, output: Path) -> ReadinessEvidence:
    record = inspect_readiness(source)
    atomic_record(output, record, create=True)
    return record


def verify_readiness(path: Path) -> ReadinessEvidence:
    proof = ReadinessEvidence.model_validate_json(path.read_bytes())
    if inspect_readiness(Path(proof.source_root)) != proof:
        raise ValueError("readiness source evidence changed")
    return proof

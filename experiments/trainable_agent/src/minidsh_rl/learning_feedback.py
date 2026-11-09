"""Post-run feedback evidence, with sealed inputs and independent partial checks."""

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, TypeAdapter

from minidsh.evaluation.tracefile import TraceRun
from minidsh.infrastructure.process import ProcessRunner
from minidsh_rl.learning_contracts import uses_curve_profile
from minidsh_rl.learning_rollout import LearningRollout, load_rollout
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest
from minidsh_rl.minirepo_analysis import feedback_revision_indices
from minidsh_rl.minirepo_verification import verify

OperationName = Literal["read", "read_many", "search", "patch", "test"]


class Operation(FrozenRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operation: OperationName
    patches: int = Field(ge=0)
    exit_code: int | None


class PartialCheck(FrozenRecord):
    patch: int
    passed: bool
    exit_code: int
    timed_out: bool


class FeedbackProof(FrozenRecord):
    run_id: str
    case_id: str
    criterion_version: str
    source_kind: str
    recovered: bool
    checked_patch_indices: tuple[int, ...]
    failed_private_patch_indices: tuple[int, ...]
    inputs: dict[str, str]
    checks: tuple[PartialCheck, ...]
    artifacts: dict[str, str]


def _artifacts(output: Path) -> dict[str, str]:
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ValueError("feedback artifact symlink rejected")
        if path.is_file() and path != output / "feedback.json":
            files[path.relative_to(output).as_posix()] = digest(path)
    return files


def _failed(checks: tuple[PartialCheck, ...]) -> tuple[int, ...]:
    return tuple(c.patch for c in checks if not c.passed and c.exit_code > 0 and not c.timed_out)


def feedback_indices(directory: Path, run_id: str) -> tuple[int, ...]:
    """Require a later actual model response, not two actions from one response."""
    operations = TypeAdapter(list[Operation]).validate_json(
        (directory / "operations.json").read_bytes()
    )
    selected = set(feedback_revision_indices([o.model_dump() for o in operations]))
    records = TraceRun.from_file(directory / "trace.jsonl", run_id=run_id).records
    names: dict[str, OperationName] = {
        "read_file": "read",
        "read_files": "read_many",
        "search_repo": "search",
        "apply_patch": "patch",
        "run_tests": "test",
    }
    observed = []
    patches, sampled_at = 0, -1
    failed_at: dict[int, int] = {}
    eligible: set[int] = set()
    for index, record in enumerate(records):
        if record.name == "model_call_completed" and record.data.get("output_tokens", 0):
            sampled_at = index
        if record.name != "tool_call_completed" or record.data.get("status") != "succeeded":
            continue
        tool = str(record.data.get("tool"))
        if tool not in names:
            raise ValueError("unexpected feedback tool")
        result = record.data.get("result")
        exit_code = result.get("exit_code") if isinstance(result, dict) else None
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("invalid feedback exit code")
        if tool == "apply_patch":
            patches += 1
            eligible.update(
                patch for patch, position in failed_at.items() if position < sampled_at < index
            )
        observed.append(Operation(operation=names[tool], patches=patches, exit_code=exit_code))
        if tool == "run_tests" and patches and type(exit_code) is int and exit_code > 0:
            failed_at[patches] = index
    if observed != operations:
        raise ValueError("feedback operations/trace mismatch")
    return tuple(sorted(selected & eligible))


async def verify_feedback(
    rollout: LearningRollout,
    source: Path,
    output: Path,
    host: ProcessRunner,
    image: str,
) -> bool:
    """Evidence remains outside the agent; no model or training operation occurs."""
    if load_rollout(rollout.case, source, rollout.policy) != rollout:
        raise ValueError("feedback requires original sealed rollout")
    indices = feedback_indices(source, rollout.run_id) if rollout.success else ()
    inputs = {
        name: digest(source / name)
        for name in (
            "learning-binding/turn_000.json",
            "trace.jsonl",
            "operations.json",
            "patch-snapshots.json",
            "candidate.json",
            "verification/result.json",
        )
    }
    if await asyncio.to_thread(output.exists):
        if not (output / "feedback.json").is_file():
            raise ValueError("incomplete feedback evidence requires audit")
        proof = FeedbackProof.model_validate_json((output / "feedback.json").read_bytes())
        if (
            proof.run_id != rollout.run_id
            or proof.case_id != rollout.case.task.case_id
            or proof.criterion_version != rollout.case.criterion_version
            or proof.source_kind != rollout.policy.kind
            or proof.inputs != inputs
            or proof.artifacts != _artifacts(output)
            or proof.checked_patch_indices != indices
            or tuple(c.patch for c in proof.checks) != indices
            or proof.failed_private_patch_indices != _failed(proof.checks)
            or proof.recovered != bool(proof.failed_private_patch_indices)
        ):
            raise ValueError("confirmed feedback proof changed")
        return proof.recovered
    snapshots = TypeAdapter(list[dict[str, str]]).validate_json(
        (source / "patch-snapshots.json").read_bytes(), strict=True
    )
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    checks = []
    for patch in indices:
        if not 1 <= patch <= len(snapshots):
            raise ValueError("feedback snapshot missing")
        candidate = {name: snapshots[patch - 1][name] for name in rollout.case.task.writable}
        verdict = await verify(
            host,
            rollout.case.task,
            candidate,
            image,
            output / f"patch_{patch:03}",
            policy="curve_pure_v1"
            if uses_curve_profile(rollout.case.dataset_version)
            else "legacy",
        )
        checks.append(
            PartialCheck(
                patch=patch,
                passed=verdict.passed,
                exit_code=verdict.exit_code,
                timed_out=verdict.timed_out,
            )
        )
    artifacts = _artifacts(output)
    if any(f"patch_{patch:03}/result.json" not in artifacts for patch in indices):
        raise ValueError("partial verification receipt missing")
    failed = _failed(tuple(checks))
    proof = FeedbackProof(
        run_id=rollout.run_id,
        case_id=rollout.case.task.case_id,
        criterion_version=rollout.case.criterion_version,
        source_kind=rollout.policy.kind,
        recovered=bool(failed),
        checked_patch_indices=indices,
        failed_private_patch_indices=tuple(failed),
        inputs=inputs,
        checks=tuple(checks),
        artifacts=artifacts,
    )
    atomic_record(output / "feedback.json", proof, create=True)
    return proof.recovered

"""Typed MiniTask trajectory bridge; no clients, updates or implicit paid entry point."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import ModelRequest
from minidsh.evaluation.tracefile import TraceRun
from minidsh.infrastructure.process import ProcessRunner
from minidsh_rl.contracts import Turn, advantages
from minidsh_rl.datum import TrainingBatch, build_batch
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.minirepo_prompts import instruction
from minidsh_rl.minirepo_rollout import run_rollout as run_minirepo
from minidsh_rl.model import Sample, SamplingBackend
from minidsh_rl.recording import FileRecordingSink

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[int, Field(ge=0, strict=True)]
GROUP_SIZE = 4
_BINDING = "learning-binding/turn_000.json"
_MAX_FILE = 16 * 1024 * 1024
_LIMITS = {
    "steps": 12,
    "tools": 12,
    "request_tokens": 6144,
    "completion": 1024,
    "generated": 3072,
    "total_tokens": 36000,
    "loop_ms": 180_000,
}


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PolicyBinding(_Closed):
    """Trusted-controller identity, not cryptographic proof of remote sampling."""

    kind: Literal["provider", "scripted_control"]
    policy_id: Annotated[str, Field(min_length=1)]
    snapshot_sha256: Digest
    tokenizer_sha256: Digest


class _Raw(_Closed):
    schema_version: Literal[1]
    run_id: str
    call_index: Count
    policy_id: str
    prompt_token_ids: tuple[Count, ...]
    completion_token_ids: tuple[Count, ...]
    old_logprobs: tuple[float, ...]
    raw_text: str
    stop_reason: str
    seed: Count
    temperature: Annotated[float, Field(ge=1.0, le=1.0)]
    top_p: Annotated[float, Field(ge=1.0, le=1.0)]


class _Started(_Closed):
    schema_version: Literal[1]
    call_index: Count
    seed: Count
    prompt_sha256: Digest


class _Returned(_Started):
    turn_sha256: Digest


class _Stopped(_Closed):
    schema_version: Literal[1]
    reason: Literal["single_submission_committed"]
    synthetic_response: Literal[True]
    sampled_tokens: Literal[0]


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    schema_version: Literal[1]
    run_id: str
    case_id: str
    family: str
    policy_id: str
    mode: Literal["single", "interactive"]
    prompt_variant: Literal["original", "tool_example"]
    read_variant: Literal["single_read", "batch_read", "batch_read_eight"]
    instruction_sha256: Digest
    seed: Count
    split: Literal["development"]
    criterion_version: Literal["minirepo_dev_v1"]
    success: bool
    status: Literal["finished", "failed", "cancelled", "budget_exhausted"]
    sampled_calls: Count
    synthetic_stop: bool
    total_tokens: Count
    generated_tokens: Count
    tool_calls: Count
    steps: Count
    failure_type: str | None = None


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    schema_version: Literal[1]
    case_id: str
    criterion_version: Literal["minirepo_dev_v1"]
    passed: bool
    exit_code: int
    timed_out: bool
    candidate_sha256: Digest


class _Final(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    duration_ms: Annotated[float, Field(ge=0)]
    steps: Count
    tokens: Count
    tool_calls: Count


class _CaseMetadata(_Closed):
    case_id: str
    family_group: str
    split: Literal["train", "test"]
    dataset_version: str
    criterion_version: str
    public_feedback_version: str
    public_fixture_sha256: Digest
    private_checker_sha256: Digest
    reference_sha256: Digest


class _Binding(_Closed):
    schema_version: Literal[2]
    call_index: Literal[0]
    case_sha256: Digest
    case_metadata: _CaseMetadata
    policy: PolicyBinding
    artifacts: dict[str, Digest]


@dataclass(frozen=True, slots=True)
class LearningRollout:
    case: LearningCase
    run_id: str
    seed: int
    policy: PolicyBinding
    success: bool
    status: str
    turns: tuple[Turn, ...]
    total_tokens: int
    harness_sha256: str
    verification_executed: bool
    local_failed_calls: int
    budget_violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LearningGroup:
    run_ids: tuple[str, ...]
    rewards: tuple[float, ...]
    advantages: tuple[float, ...]
    batch: TrainingBatch | None


def turn_digest(value: Turn | tuple[int, ...] | dict[str, str]) -> str:
    """Hash original arrays, never parsed/re-encoded model text."""
    encoded = (
        {"prompt": value.prompt, "completion": value.completion, "logprobs": value.logprobs}
        if isinstance(value, Turn)
        else value
    )
    return hashlib.sha256(json.dumps(encoded, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE:
        raise ValueError("missing, symlinked or oversized artifact")
    return path.read_bytes()


def _artifacts(directory: Path) -> dict[str, str]:
    result = {}
    if directory.is_symlink():
        raise ValueError("symlinked artifact directory")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinked artifact")
        if path.is_file() and path.relative_to(directory).as_posix() != _BINDING:
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(_read(path)).hexdigest()
    return result


def _case_metadata(case: LearningCase) -> _CaseMetadata:
    return _CaseMetadata(
        case_id=case.task.case_id,
        family_group=case.family_group,
        split=case.split,
        dataset_version=case.dataset_version,
        criterion_version=case.criterion_version,
        public_feedback_version=case.public_feedback_version,
        public_fixture_sha256=turn_digest(dict(case.task.files)),
        private_checker_sha256=hashlib.sha256(case.task.hidden_checks.encode()).hexdigest(),
        reference_sha256=turn_digest(dict(case.task.reference)),
    )


def _case_digest(case: LearningCase) -> str:
    return turn_digest(_case_metadata(case).model_dump(mode="json"))


def _turns(directory: Path, result: _Result) -> tuple[Turn, ...]:
    files = sorted((directory / "tokens").glob("turn_*.json"))
    started = sorted((directory / "sample-started").glob("turn_*.json"))
    returned = sorted((directory / "sample-returned").glob("turn_*.json"))
    if (
        len(files) != result.sampled_calls
        or len(started) != len(files)
        or len(returned) != len(files)
    ):
        raise ValueError("unknown sampling outcome or incomplete raw trajectory")
    turns = []
    for index, (raw_path, a, b) in enumerate(zip(files, started, returned, strict=True)):
        expected_name = f"turn_{index:03}.json"
        if any(p.name != expected_name for p in (raw_path, a, b)):
            raise ValueError("noncontiguous raw call indices")
        raw = _Raw.model_validate_json(_read(raw_path))
        begin, end = _Started.model_validate_json(_read(a)), _Returned.model_validate_json(_read(b))
        turn = Turn(raw.prompt_token_ids, raw.completion_token_ids, raw.old_logprobs)
        if (
            raw.run_id != result.run_id
            or raw.policy_id != result.policy_id
            or raw.call_index != index
            or begin.call_index != index
            or end.call_index != index
            or raw.seed != result.seed + index
            or begin.seed != raw.seed
            or end.seed != raw.seed
            or begin.prompt_sha256 != turn_digest(turn.prompt)
            or end.prompt_sha256 != begin.prompt_sha256
            or end.turn_sha256 != turn_digest(turn)
        ):
            raise ValueError("raw sampling identity/token binding mismatch")
        turns.append(turn)
    if (
        sum(len(t.prompt) + len(t.completion) for t in turns) != result.total_tokens
        or sum(len(t.completion) for t in turns) != result.generated_tokens
    ):
        raise ValueError("raw token usage mismatch")
    return tuple(turns)


def _validate_trace(
    directory: Path, result: _Result, turns: tuple[Turn, ...]
) -> tuple[int, tuple[str, ...]]:
    # TraceRun is a typed view; additionally reject foreign runs and missing/duplicate terminals.
    trace_path = directory / "trace.jsonl"
    for line in _read(trace_path).decode().splitlines():
        if line.strip() and json.loads(line).get("run_id") != result.run_id:
            raise ValueError("foreign run in trajectory")
    trace = TraceRun.from_file(trace_path, run_id=result.run_id)
    if len(trace.by_name("run_finished")) != 1 or trace.status() != result.status:
        raise ValueError("missing/inconsistent terminal Trace")
    if any(a.step_id > b.step_id for a, b in zip(trace.records, trace.records[1:], strict=False)):
        raise ValueError("nonmonotonic trajectory steps")
    complete = trace.by_name("model_call_completed")
    failed = trace.by_name("model_call_failed")
    if len(trace.by_name("model_call_started")) != len(complete) + len(failed):
        raise ValueError("incomplete model call Trace")
    synthetic = [
        e for e in complete if e.data.get("input_tokens") == e.data.get("output_tokens") == 0
    ]
    if len(synthetic) != int(result.synthetic_stop) or (
        result.synthetic_stop
        and (result.mode != "single" or not (directory / "protocol-stop.json").is_file())
    ):
        raise ValueError("synthetic response binding mismatch")
    if result.synthetic_stop:
        _Stopped.model_validate_json(_read(directory / "protocol-stop.json"))
    sampled = [e for e in complete if e not in synthetic]
    if len(sampled) != len(turns) or any(
        e.data.get("input_tokens") != len(t.prompt)
        or e.data.get("output_tokens") != len(t.completion)
        or e.data.get("usage_known") is not True
        for e, t in zip(sampled, turns, strict=True)
    ):
        raise ValueError("Trace/raw sampling usage mismatch")
    final_record = trace.final()
    if final_record is None:
        raise ValueError("missing terminal budget evidence")
    final = _Final.model_validate(final_record.data)
    if (
        final.steps != result.steps
        or final.tokens != result.total_tokens
        or final.tool_calls != result.tool_calls
    ):
        raise ValueError("terminal budget/usage mismatch")
    observed = {
        "steps": final.steps,
        "tools": final.tool_calls,
        "request_tokens": max((len(t.prompt) + len(t.completion) for t in turns), default=0),
        "completion": max((len(t.completion) for t in turns), default=0),
        "generated": result.generated_tokens,
        "total_tokens": result.total_tokens,
        "loop_ms": final.duration_ms,
    }
    return len(failed), tuple(name for name, count in observed.items() if count > _LIMITS[name])


def _load(case: LearningCase, directory: Path, policy: PolicyBinding) -> LearningRollout:
    result = _Result.model_validate_json(_read(directory / "result.json"))
    if (
        result.case_id != case.task.case_id
        or result.family != case.task.family
        or result.policy_id != policy.policy_id
    ):
        raise ValueError("case or policy mismatch")
    expected_instruction = hashlib.sha256(
        instruction(case.task, result.mode, result.prompt_variant).encode()
    ).hexdigest()
    if result.instruction_sha256 != expected_instruction:
        raise ValueError("instruction identity mismatch")
    if policy.kind == "provider" and (
        result.mode != "interactive"
        or result.prompt_variant != "original"
        or result.read_variant != "batch_read_eight"
    ):
        raise ValueError("provider rollout does not match learning Harness")
    turns = _turns(directory, result)
    local_failed, violations = _validate_trace(directory, result, turns)
    candidate_path = directory / "candidate.json"
    executed = False
    if candidate_path.exists():
        candidate = json.loads(_read(candidate_path))
        if (
            not isinstance(candidate, dict)
            or set(candidate) != set(case.task.writable)
            or any(not isinstance(v, str) for v in candidate.values())
        ):
            raise ValueError("invalid captured candidate")
        verdict = _Verdict.model_validate_json(_read(directory / "verification/result.json"))
        if (
            verdict.case_id != case.task.case_id
            or verdict.candidate_sha256 != turn_digest(candidate)
            or verdict.passed != result.success
        ):
            raise ValueError("independent candidate/reward mismatch")
        if verdict.passed and (verdict.exit_code != 0 or verdict.timed_out):
            raise ValueError("inconsistent successful verdict")
        executed = verdict.exit_code != -1
    elif result.success or result.failure_type not in {
        "forbidden_workspace_change",
        "candidate_capture_failed",
        "container_terminated",
    }:
        raise ValueError("missing independent outcome evidence")
    harness = {
        "instruction": expected_instruction,
        "mode": result.mode,
        "read": result.read_variant,
        "cap": "1024",
        "context": "6144",
        "generated": "3072",
        "total": "36000",
        "steps": "12",
    }
    return LearningRollout(
        case,
        result.run_id,
        result.seed,
        policy,
        result.success,
        result.status,
        turns,
        result.total_tokens,
        turn_digest(harness),
        executed,
        local_failed,
        violations,
    )


async def seal_rollout(case: LearningCase, directory: Path, policy: PolicyBinding) -> None:
    """Validate completed files then atomically publish a separate immutable binding."""
    artifacts = await asyncio.to_thread(_artifacts, directory)
    await asyncio.to_thread(_load, case, directory, policy)
    if artifacts != await asyncio.to_thread(_artifacts, directory):
        raise ValueError("artifacts changed during validation")
    binding = _Binding(
        schema_version=2,
        call_index=0,
        case_sha256=_case_digest(case),
        case_metadata=_case_metadata(case),
        policy=policy,
        artifacts=artifacts,
    )
    await FileRecordingSink(directory / "learning-binding").append(binding.model_dump(mode="json"))


def load_rollout(case: LearningCase, directory: Path, policy: PolicyBinding) -> LearningRollout:
    """Read-only reload; refuse changed files, versions or controller identity."""
    artifacts = _artifacts(directory)
    binding = _Binding.model_validate_json(_read(directory / _BINDING))
    if (
        binding.case_sha256 != _case_digest(case)
        or binding.case_metadata != _case_metadata(case)
        or binding.policy != policy
    ):
        raise ValueError("learning case/version/policy binding mismatch")
    if binding.artifacts != artifacts:
        raise ValueError("sealed artifact mismatch")
    return _load(case, directory, policy)


@dataclass(slots=True)
class _ObservedBackend:
    inner: SamplingBackend
    directory: Path
    index: int = 0

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        return self.inner.render(request)

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        index = self.index
        self.index += 1
        receipt = {
            "schema_version": 1,
            "call_index": index,
            "seed": seed,
            "prompt_sha256": turn_digest(prompt),
        }
        await FileRecordingSink(self.directory / "sample-started").append(receipt)
        sample = await self.inner.sample(prompt, limit, seed)
        await FileRecordingSink(self.directory / "sample-returned").append(
            receipt | {"turn_sha256": turn_digest(sample.turn)}
        )
        return sample


async def run_learning_rollout(  # noqa: PLR0913 - explicit isolated dependencies
    case: LearningCase,
    backend: SamplingBackend,
    *,
    policy: PolicyBinding,
    run_id: str,
    seed: int,
    output: Path,
    host: ProcessRunner,
    image: str,
    mode: Literal["single", "interactive"] = "interactive",
) -> LearningRollout:
    """Caller supplies the backend; this bridge neither creates clients nor admits spend."""
    if policy.kind == "provider" and mode != "interactive":
        raise ValueError("provider training requires interactive mode")
    await run_minirepo(
        case.task,
        _ObservedBackend(backend, output),
        policy_id=policy.policy_id,
        run_id=run_id,
        seed_value=seed,
        mode=mode,
        output=output,
        host=host,
        image=image,
        max_output_per_call=1024,
        prompt_variant="original",
        read_variant="batch_read_eight",
    )
    await seal_rollout(case, output, policy)
    return await asyncio.to_thread(load_rollout, case, output, policy)


def assemble_group(rollouts: tuple[LearningRollout, ...]) -> LearningGroup:
    """G4 from one policy/case/Harness; never remove failures or fabricate empty turns."""
    if len(rollouts) != GROUP_SIZE:
        raise ValueError("exactly four declared rollouts required")
    first = rollouts[0]
    if (
        len({r.run_id for r in rollouts}) != GROUP_SIZE
        or len({r.seed for r in rollouts}) != GROUP_SIZE
    ):
        raise ValueError("duplicate run/seed identity")
    if any(r.budget_violations for r in rollouts):
        raise ValueError("budget violation blocks training; retain original outcome and usage")
    if any(
        r.case != first.case
        or r.case.split != "train"
        or r.policy != first.policy
        or r.policy.kind != "provider"
        or r.harness_sha256 != first.harness_sha256
        for r in rollouts
    ):
        raise ValueError("cross-case/policy/Harness or non-provider training group")
    rewards = tuple(float(r.success) for r in rollouts)
    relative = advantages(rewards)
    batch = (
        build_batch(tuple(r.turns for r in rollouts), relative)
        if any(r.turns for r in rollouts)
        else None
    )
    return LearningGroup(tuple(r.run_id for r in rollouts), rewards, relative, batch)

"""Unchanged AgentLoop with structured container tools and submission-mode control."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget, Message, ModelRequest, ModelResponse, ToolDefinition
from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessRunner
from minidsh.tools.runtime import ToolError
from minidsh_rl.minirepo_prompts import PromptVariant
from minidsh_rl.minirepo_prompts import instruction as assemble_instruction
from minidsh_rl.minirepo_tasks import MiniTask
from minidsh_rl.minirepo_tools import ReadVariant, RepoSession, RepoTool, ToolName
from minidsh_rl.minirepo_verification import CurvePolicy, seed, verify
from minidsh_rl.model import RecordingModel, SamplingBackend
from minidsh_rl.recording import FileRecordingSink


@dataclass(slots=True)
class SubmissionModel:
    """A labelled protocol stop, never fabricated sampled tokens or policy actions."""

    recording: RecordingModel
    session: RepoSession
    mode: Literal["single", "interactive"]
    output: Path

    def count_tokens(self, messages: tuple[Message, ...], tools: tuple[ToolDefinition, ...]) -> int:
        return self.recording.count_tokens(messages, tools)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.mode == "single" and self.session.patches:
            (self.output / "protocol-stop.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reason": "single_submission_committed",
                        "synthetic_response": True,
                        "sampled_tokens": 0,
                    }
                )
            )
            return ModelResponse(content="Submission committed; independent verification follows.")
        return await self.recording.complete(request)


async def run_rollout(  # noqa: PLR0913 - explicit isolated experiment composition
    task: MiniTask,
    backend: SamplingBackend,
    *,
    policy_id: str,
    run_id: str,
    seed_value: int,
    mode: Literal["single", "interactive"],
    output: Path,
    host: ProcessRunner,
    image: str,
    max_output_per_call: int = 512,
    prompt_variant: PromptVariant = "original",
    read_variant: ReadVariant = "single_read",
    candidate_policy: CurvePolicy = "legacy",
) -> dict[str, object]:
    if max_output_per_call not in (512, 1024):
        raise ValueError("unsupported development output cap")
    if read_variant not in ("single_read", "batch_read", "batch_read_eight"):
        raise ValueError("unsupported read variant")
    started = time.monotonic()
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    recording = RecordingModel(
        backend,
        FileRecordingSink(output / "tokens"),
        policy_id,
        run_id,
        seed_value,
        max_output=max_output_per_call,
        context_limit=6144,
        generated_limit=3072,
        total_limit=36000,
    )
    candidate: dict[str, str] | None = None
    failure_type = None
    async with DockerSandbox(
        host,
        image=image,
        resources=ContainerResources(
            platform="linux/arm64",
            cpus=1,
            memory_mb=1024,
            pids_limit=128,
        ),
    ) as box:
        await seed(box, dict(task.files))
        runner = box.runner("/app", CommandPolicy(frozenset({"python3"})))
        session = RepoSession(
            runner,
            task,
            1 if mode == "single" else 12,
            batch_path_limit=8 if read_variant == "batch_read_eight" else 4,
            candidate_policy=candidate_policy,
        )
        model = SubmissionModel(recording, session, mode, output)
        instruction = assemble_instruction(task, mode, prompt_variant)
        runtime = build_local_runtime(
            workspace=output,
            model=model,
            budget=AgentBudget(
                max_steps=12, max_tokens=36000, max_tool_calls=12, max_duration_seconds=180
            ),
            options=RuntimeOptions(
                builtin_tools_enabled=False,
                allow_shell=True,
                allow_write=True,
                error_recovery_enabled=False,
                normalize_tool_results=True,
                artifact_offload=False,
                tool_max_retries=0,
                context_input_budget=6144,
                tool_output_limit=6000,
                system_prompt=instruction,
            ),
            components=RuntimeComponents(
                runner=runner,
                tools=tuple(
                    RepoTool(session, cast(ToolName, name))
                    for name in (
                        ("read_file", "search_repo", "apply_patch", "run_tests", "read_files")
                        if read_variant != "single_read"
                        else ("read_file", "search_repo", "apply_patch", "run_tests")
                    )
                ),
            ),
        )
        runtime.events.subscribe("*", JsonlTraceWriter(output / "trace.jsonl"))
        result = await runtime.agent.run(task.specification, run_id=run_id)
        if box.alive:
            try:
                files = await session.capture()
                original = dict(task.files)
                if any(
                    files[name] != original[name] for name in original if name not in task.writable
                ):
                    failure_type = "forbidden_workspace_change"
                else:
                    candidate = {name: files[name] for name in task.writable}
            except (ValueError, RuntimeError, ToolError, OSError):
                failure_type = "candidate_capture_failed"
        else:
            failure_type = "container_terminated"
        (output / "patch-snapshots.json").write_text(json.dumps(session.snapshots, indent=2))
        (output / "operations.json").write_text(json.dumps(session.operations, indent=2))
        patch_count = session.patches
    passed = False
    candidate_hash = None
    if candidate is not None:
        (output / "candidate.json").write_text(json.dumps(candidate, indent=2))
        candidate_hash = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
        verdict = await verify(
            host, task, candidate, image, output / "verification", policy=candidate_policy
        )
        passed = verdict.passed
        if not passed:
            failure_type = "independent_verification_failed"
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": task.case_id,
        "family": task.family,
        "policy_id": policy_id,
        "mode": mode,
        "prompt_variant": prompt_variant,
        "read_variant": read_variant,
        "batch_path_limit": session.batch_path_limit,
        "batch_read_calls": sum(x["operation"] == "read_many" for x in session.operations),
        "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
        "seed": seed_value,
        "split": "development",
        "criterion_version": "minirepo_dev_v1",
        "success": passed,
        "status": result.status.value,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "patches": patch_count,
        "total_tokens": recording.consumed,
        "generated_tokens": recording.generated,
        "sampled_calls": len(recording.turns),
        "synthetic_stop": (output / "protocol-stop.json").exists(),
        "duration_ms": (time.monotonic() - started) * 1000,
        "candidate_hash": candidate_hash,
        "failure_type": failure_type,
        "loop_failure_reason": result.failure_reason,
    }
    if candidate_policy != "legacy":
        record["candidate_policy"] = candidate_policy
    (output / "result.json").write_text(json.dumps(record, indent=2))
    return record

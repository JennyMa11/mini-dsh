"""One coding rollout through the unchanged AgentLoop and isolated ToolRuntime."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget
from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessRunner
from minidsh.tools.builtin import ShellTool
from minidsh_rl.contracts import Turn
from minidsh_rl.model import RecordingModel, SamplingBackend
from minidsh_rl.recording import FileRecordingSink
from minidsh_rl.tasks import Task, public_fixture
from minidsh_rl.tasks_v2 import ExpansionTask, public_files
from minidsh_rl.verification import verify
from minidsh_rl.verification_v2 import verify as verify_expansion

# Trusted orchestrator program; no model text is interpolated into it.
_CAPTURE = """
import json
from pathlib import Path
root = Path('/app')
files = {}
for p in root.rglob('*'):
    if '__pycache__' in p.parts:
        continue
    if p.is_symlink():
        raise ValueError('symlink in candidate')
    if p.is_file():
        if p.stat().st_size > 65536:
            raise ValueError('oversized candidate')
        files[str(p.relative_to(root))] = p.read_text()
print(json.dumps(files))
"""


class _Capture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class Rollout:
    """Independent success and raw trajectory, retaining failed/limited runs."""

    run_id: str
    success: bool
    status: str
    turns: tuple[Turn, ...]
    tool_calls: int
    total_tokens: int
    duration_ms: float
    candidate_hash: str | None
    failure_type: str | None


async def run_rollout(  # noqa: PLR0913 - explicit isolated dependencies
    task: Task,
    backend: SamplingBackend,
    *,
    policy_id: str,
    run_id: str,
    seed: int,
    output: Path,
    host: ProcessRunner,
    image: str,
    instruction_suffix: str = "",
) -> Rollout:
    """Run once in a fresh artifact directory; capture and verify after agent exit."""
    started = time.monotonic()
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    fixture = public_files(task) if isinstance(task, ExpansionTask) else public_fixture(task)
    fixture_dir = output / "fixture"
    await asyncio.to_thread(fixture_dir.mkdir)
    for name, text in fixture.items():
        await asyncio.to_thread((fixture_dir / name).write_text, text)
    model = RecordingModel(backend, FileRecordingSink(output / "tokens"), policy_id, run_id, seed)
    candidate = None
    failure_type = None
    async with DockerSandbox(
        host,
        image=image,
        resources=ContainerResources(
            platform="linux/arm64", cpus=1, memory_mb=1024, pids_limit=128
        ),
    ) as box:
        # Create files as the container user: docker cp can preserve host UID,
        # which becomes unwritable when CAP_DAC_OVERRIDE is dropped.
        seeded = await box.execute(
            (
                "python3",
                "-c",
                "import json,sys; from pathlib import Path; "
                "p=Path('/app'); p.mkdir(exist_ok=True); "
                "[(p/name).write_text(text) for name,text in json.loads(sys.argv[1]).items()]",
                json.dumps(fixture),
            )
        )
        if seeded.exit_code or seeded.timed_out:
            raise ValueError("container fixture seeding failed")
        runner = box.runner("/app", CommandPolicy(frozenset({"python3", "cat", "ls"})))
        runtime = build_local_runtime(
            workspace=output,
            model=model,
            budget=AgentBudget(
                max_steps=8, max_tokens=24000, max_tool_calls=8, max_duration_seconds=120
            ),
            options=RuntimeOptions(
                builtin_tools_enabled=False,
                allow_shell=True,
                allow_write=True,
                error_recovery_enabled=False,
                normalize_tool_results=True,
                artifact_offload=False,
                tool_max_retries=0,
                context_input_budget=4096,
                system_prompt=(
                    "Repair the Python bug using tools. You have at most 8 tool steps. "
                    "The shell tool executes an argv array directly, without a shell interpreter. "
                    "Allowed programs: python3, cat, ls. Omit cwd or set cwd to '.'; "
                    "absolute cwd values are rejected. Paths are relative to the workspace. "
                    "Inspect the implementation and the visible test together in one call. "
                    "Only implementation.py may change; preserve the callable interface. "
                    "The implementation must use pure Python built-ins: "
                    "no imports, I/O or introspection. "
                    "To edit a file use python3 -c, for example argv "
                    '["python3","-c","from pathlib import Path; '
                    "p=Path('implementation.py'); "
                    "p.write_text(p.read_text().replace('OLD_TEXT','NEW_TEXT'))\"]. "
                    "Choose OLD_TEXT and NEW_TEXT from the actual bug, not this example. "
                    "Do not use echo, bash, redirection, or single-quoted JSON strings. "
                    "Tool argv parameters must be valid JSON arrays with double quotes. "
                    'After editing run ["python3","smoke_test.py"]. '
                    "Finish briefly once it passes. Avoid rereading unchanged files. "
                    "Only one tool call per assistant turn."
                )
                + instruction_suffix,
            ),
            components=RuntimeComponents(runner=runner, tools=(ShellTool(runner),)),
        )
        runtime.events.subscribe("*", JsonlTraceWriter(output / "trace.jsonl"))
        result = await runtime.agent.run(task.specification, run_id=run_id)
        if box.alive:
            capture = await box.execute(("python3", "-c", _CAPTURE), cwd="/app", timeout_seconds=10)
            if capture.exit_code != 0 or capture.timed_out:
                failure_type = "candidate_capture_failed"
            else:
                try:
                    captured = _Capture.model_validate({"files": json.loads(capture.stdout)}).files
                    if set(captured) != set(fixture) or any(
                        captured[name] != text
                        for name, text in fixture.items()
                        if name != "implementation.py"
                    ):
                        failure_type = "forbidden_workspace_change"
                    else:
                        candidate = captured["implementation.py"]
                except ValueError:
                    failure_type = "candidate_capture_invalid"
    passed = False
    candidate_hash = None
    if candidate is not None:
        candidate_hash = hashlib.sha256(candidate.encode()).hexdigest()
        await asyncio.to_thread((output / "candidate.py").write_text, candidate)
        if isinstance(task, ExpansionTask):
            verdict = await verify_expansion(host, task, candidate, image, output / "verification")
        else:
            verdict = await verify(host, task, candidate, image, output / "verification")
        passed = verdict.passed
        if not passed:
            failure_type = "independent_verification_failed"
    duration = (time.monotonic() - started) * 1000
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": task.case_id,
        "policy_id": policy_id,
        "success": passed,
        "status": result.status.value,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "total_tokens": result.token_usage.total_tokens,
        "duration_ms": duration,
        "candidate_hash": candidate_hash,
        "failure_type": failure_type,
        "loop_failure_reason": result.failure_reason,
        "split": task.split,
        "seed": seed,
        "instruction_suffix": instruction_suffix,
        "criterion_version": "expansion_v2"
        if isinstance(task, ExpansionTask)
        else "lightweight_v2",
    }
    await asyncio.to_thread((output / "result.json").write_text, json.dumps(record, indent=2))
    return Rollout(
        run_id,
        passed,
        result.status.value,
        tuple(model.turns),
        result.tool_calls,
        result.token_usage.total_tokens,
        duration,
        candidate_hash,
        failure_type,
    )

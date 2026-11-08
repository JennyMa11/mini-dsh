"""Independent candidate verification using a fresh, networkless Docker sandbox."""

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.benchmark.public_lite import seed_container
from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import ProcessRunner
from minidsh_rl.task_rules import candidate_allowed, verifier_code
from minidsh_rl.tasks import Task


@dataclass(frozen=True, slots=True)
class Verdict:
    """A verifier fact, distinct from the Agent Loop terminal state."""

    passed: bool
    exit_code: int
    timed_out: bool
    duration_ms: float


def verifier_files(task: Task, candidate: str, marker: str = "verified") -> dict[str, str]:
    """Create a sealed verifier; no reference answer enters the candidate tree."""
    return {
        "solution.py": "from implementation import solve\n",
        "implementation.py": candidate,
        "verify.py": verifier_code(task, marker),
    }


async def verify(
    host: ProcessRunner, task: Task, candidate: str, image: str, logs: Path
) -> Verdict:
    """Run frozen checks in a disposable container, never the agent workspace."""
    started = time.monotonic()
    marker = "verified_" + secrets.token_hex(16)
    await asyncio.to_thread(logs.mkdir, parents=True, exist_ok=True)
    if not candidate_allowed(candidate):
        (logs / "result.json").write_text(
            json.dumps(
                {
                    "case_id": task.case_id,
                    "passed": False,
                    "criterion_version": "lightweight_v2",
                    "error": "candidate_policy_rejected",
                }
            )
        )
        return Verdict(
            passed=False,
            exit_code=-1,
            timed_out=False,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    with TemporaryDirectory(prefix="minidsh-rl-verifier-") as temporary:
        seed = Path(temporary)
        for name, text in verifier_files(task, candidate, marker).items():
            await asyncio.to_thread((seed / name).write_text, text)
        async with DockerSandbox(
            host,
            image=image,
            resources=ContainerResources(platform="linux/arm64", cpus=1, memory_mb=256),
        ) as box:
            await seed_container(host, box, seed)
            result = await box.execute(("python3", "verify.py"), cwd="/app", timeout_seconds=10)
    verdict = Verdict(
        result.exit_code == 0 and not result.timed_out and marker in result.stdout.splitlines(),
        result.exit_code,
        result.timed_out,
        (time.monotonic() - started) * 1000,
    )
    # Bounded process captures are already enforced by the infrastructure adapter.
    (logs / "stdout.txt").write_text(result.stdout)
    (logs / "stderr.txt").write_text(result.stderr)
    (logs / "result.json").write_text(
        json.dumps(
            {
                "case_id": task.case_id,
                "passed": verdict.passed,
                "exit_code": verdict.exit_code,
                "timed_out": verdict.timed_out,
                "duration_ms": verdict.duration_ms,
                "criterion_version": "lightweight_v2",
            },
            indent=2,
        )
    )
    return verdict

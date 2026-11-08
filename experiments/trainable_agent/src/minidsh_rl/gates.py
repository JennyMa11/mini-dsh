"""Reproduce deterministic candidate-verifier gates without any model calls."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.tasks import smoke_tasks, tasks
from minidsh_rl.verification import verify


async def run_gates(output: Path, image: str, *, smoke: bool = False) -> None:
    """Verify every bug and reference in separate fresh containers."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
    rows: list[dict[str, str | bool]] = []
    for task in smoke_tasks() if smoke else tasks():
        for kind, source, expected in (
            ("broken", task.broken, False),
            ("reference", task.reference, True),
        ):
            verdict = await verify(host, task, source, image, output / task.case_id / kind)
            rows.append(
                {
                    "case_id": task.case_id,
                    "kind": kind,
                    "passed": verdict.passed,
                    "expected": expected,
                }
            )
            if verdict.passed != expected:
                raise RuntimeError(f"gate failed: {task.case_id}/{kind}")
    text = json.dumps({"schema_version": 1, "image": image, "checks": rows}, indent=2)
    await asyncio.to_thread((output / "summary.json").write_text, text)
    sys.stdout.write(f"Passed {len(rows)} independent container checks\n")


def main() -> None:
    """Explicit native-arm64 gate command; use a fresh artifact directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a fresh output directory to preserve prior evidence")
    asyncio.run(run_gates(args.output.resolve(), args.image, smoke=args.smoke))


if __name__ == "__main__":
    main()

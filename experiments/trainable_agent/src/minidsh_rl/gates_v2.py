"""Freeze and execute all v2 semantic gates in fresh networkless containers."""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.tasks_v2 import expansion_tasks, public_files, verifier_source
from minidsh_rl.verification_v2 import verify

PROGRESS_INTERVAL = 32


def mutating_candidate(reference: str) -> str:
    """A deterministic wrong solution that preserves values but mutates a list input."""
    return (
        reference.replace("solve(", "_inner(")
        + """
def solve(*args):
    result = _inner(*args)
    for value in args:
        if isinstance(value, list):
            value.append(None)
            break
    return result
"""
    )


async def gates(output: Path, image: str) -> None:
    """Declare checks before running; keep unexecuted rows when a gate fails."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    tasks = expansion_tasks()
    sources = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            Path(__file__),
            Path("experiments/trainable_agent/src/minidsh_rl/tasks_v2.py"),
            Path("experiments/trainable_agent/src/minidsh_rl/verification_v2.py"),
            Path("experiments/trainable_agent/src/minidsh_rl/task_rules.py"),
            Path("src/minidsh/infrastructure/containers.py"),
        ]
    }
    manifest = []
    rows: list[dict[str, object]] = []
    candidates: dict[tuple[str, str], str] = {}
    for task in tasks:
        public = public_files(task)
        manifest.append(
            {
                "case_id": task.case_id,
                "family": task.family,
                "split": task.split,
                "difficulty_target": ("easy", "medium", "medium", "hard")[task.variant],
                "difficulty_measured": None,
                "criterion_version": "expansion_v2",
                "criterion_hash": hashlib.sha256(
                    verifier_source(task, "marker").encode()
                ).hexdigest(),
                "fixture_hash": hashlib.sha256(
                    json.dumps(public, sort_keys=True).encode()
                ).hexdigest(),
                "reference_hash": hashlib.sha256(task.reference.encode()).hexdigest(),
                "invalid_case_count": len(task.invalid_inputs),
                "oracle_case_count": len(task.examples),
                "preserve_inputs": task.preserve_inputs,
            }
        )
        variants = [
            ("broken", task.broken, False),
            ("reference", task.reference, True),
            ("constant_wrong", "def solve(*args): return " + repr(task.examples[0][1]), False),
        ]
        if any(isinstance(arg, list) for args, _ in task.examples for arg in args):
            variants.append(("mutates_input", mutating_candidate(task.reference), False))
        for kind, source, expected in variants:
            candidates[task.case_id, kind] = source
            rows.append(
                {"case_id": task.case_id, "kind": kind, "expected": expected, "status": "not_run"}
            )
    await asyncio.to_thread((output / "manifest.json").write_text, json.dumps(manifest, indent=2))
    await asyncio.to_thread((output / "sources.json").write_text, json.dumps(sources, indent=2))
    queue = output / "queue.json"
    await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
    task_by_id = {t.case_id: t for t in tasks}
    host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
    for index, row in enumerate(rows):
        case_id, kind = str(row["case_id"]), str(row["kind"])
        row["status"] = "running"
        await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
        result = await verify(
            host, task_by_id[case_id], candidates[case_id, kind], image, output / case_id / kind
        )
        row.update({"status": "completed", "passed": result.passed})
        await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
        if result.passed is not row["expected"]:
            raise ValueError("independent gate mismatch: " + case_id + "/" + kind)
        if (index + 1) % PROGRESS_INTERVAL == 0:
            sys.stdout.write(f"{index + 1}/{len(rows)} independent gates completed\n")
            sys.stdout.flush()
    await asyncio.to_thread(
        (output / "summary.json").write_text,
        json.dumps(
            {
                "declared": len(rows),
                "completed": len(rows),
                "passed": True,
                "coding_rollouts": 0,
                "model_calls": 0,
                "image": image,
                "families": len({t.family for t in tasks}),
                "tasks": len(tasks),
                "source": "local deterministic fixtures in isolated containers",
            },
            indent=2,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(gates(args.output.resolve(), args.image))


if __name__ == "__main__":
    main()

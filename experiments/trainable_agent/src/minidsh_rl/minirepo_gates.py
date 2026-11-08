# ruff: noqa: ASYNC240, FBT003 - offline fixed fixture gate enumeration
"""Offline fixture/oracle gates plus individually restored bug mutations."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_verification import IMAGE, verify


async def gates(output: Path) -> None:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    rows = []
    for task in development_tasks():
        reference = dict(task.reference)
        broken = {name: dict(task.files)[name] for name in task.writable}
        candidates = [("reference", reference, True), ("broken", broken, False)]
        constant = reference | {"service.py": "def solve(payload):\n    return {}\n"}
        candidates.append(("constant", constant, False))
        mutation = 0
        for name, original_source in reference.items():
            gold_lines, bad_lines = original_source.splitlines(True), broken[name].splitlines(True)
            if len(gold_lines) != len(bad_lines):
                raise ValueError("unexpected mutation source alignment")
            for index, (gold, bad) in enumerate(zip(gold_lines, bad_lines, strict=True)):
                if gold != bad:
                    lines = list(gold_lines)
                    lines[index] = bad
                    candidates.append(
                        (f"mutation_{mutation}", reference | {name: "".join(lines)}, False)
                    )
                    mutation += 1
        candidates.extend([("reference_public", reference, True), ("broken_public", broken, False)])
        for label, candidate, expected in candidates:
            result = await verify(
                DockerHost(),
                task,
                candidate,
                IMAGE,
                output / f"{task.case_id}_{label}",
                checks="public" if label.endswith("_public") else "private",
            )
            row = {
                "case_id": task.case_id,
                "kind": label,
                "expected": expected,
                "passed": result.passed,
            }
            rows.append(row)
            if result.passed != expected:
                (output / "failed.json").write_text(json.dumps(row, indent=2))
                raise ValueError("independent task gate failed")
    sources = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path("experiments/trainable_agent/src/minidsh_rl").glob("minirepo*.py")
    }
    (output / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": True,
                "declared": len(rows),
                "completed": len(rows),
                "image": IMAGE,
                "rows": rows,
                "source_sha256": sources,
                "new_model_calls": 0,
                "fixture_sha256": {
                    t.case_id: hashlib.sha256(json.dumps(t.files).encode()).hexdigest()
                    for t in development_tasks()
                },
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(gates(args.output))


if __name__ == "__main__":
    main()

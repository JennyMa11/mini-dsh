"""The eight predeclared engineering calibration rollouts, without training."""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.budget import BudgetStore
from minidsh_rl.pytrio_backend import PyTrioBackend, Tokenizer
from minidsh_rl.rollout import run_rollout
from minidsh_rl.tasks import smoke_tasks


async def calibrate(output: Path, budget_path: Path, image: str) -> None:
    """Freeze all eight queue rows before the first model call; do not resample failures."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    cases = smoke_tasks()
    rows: list[dict[str, object]] = [
        {
            "run_id": f"calibration_{i:02}",
            "case_id": task.case_id,
            "seed": 2017 + i,
            "status": "not_run",
        }
        for i, task in enumerate(case for case in cases for _ in range(2))
    ]
    queue_path = output / "queue.json"
    await asyncio.to_thread(queue_path.write_text, json.dumps(rows, indent=2))
    with BudgetStore(budget_path, Decimal("50")) as budget:
        service = trio.ServiceClient()
        sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B")
        backend = PyTrioBackend(
            sampler, cast("Tokenizer", sampler.get_tokenizer()), budget, "base_qwen35_4b"
        )
        identity = backend.identity() | {"sampling_run_cuid": sampler.sampling_run_cuid}
        await asyncio.to_thread((output / "policy.json").write_text, json.dumps(identity, indent=2))
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        for i, row in enumerate(rows):
            task = cases[i // 2]
            row["status"] = "running"
            await asyncio.to_thread(queue_path.write_text, json.dumps(rows, indent=2))
            try:
                result = await run_rollout(
                    task,
                    backend,
                    policy_id=backend.policy_id,
                    run_id=str(row["run_id"]),
                    seed=int(str(row["seed"])),
                    output=output / str(row["run_id"]),
                    host=host,
                    image=image,
                )
            except Exception as error:
                row["status"] = "infrastructure_failed"
                row["error_type"] = type(error).__name__
                await asyncio.to_thread(queue_path.write_text, json.dumps(rows, indent=2))
                raise
            row.update(
                {
                    "status": result.status,
                    "success": result.success,
                    "tokens": result.total_tokens,
                    "tool_calls": result.tool_calls,
                }
            )
            await asyncio.to_thread(queue_path.write_text, json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(calibrate(args.output.resolve(), args.budget.resolve(), args.image))


if __name__ == "__main__":
    main()

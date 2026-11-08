"""Frozen paired Base/final-RL evaluation with the same Harness and task verifier."""

import argparse
import asyncio
import hashlib
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
from minidsh_rl.tasks import tasks

POLICIES = ("base", "rl")


def evaluation_queue() -> list[dict[str, object]]:
    """80 single-attempt rows, paired by case/repeat and alternating policy order."""
    rows: list[dict[str, object]] = []
    pair = 0
    for split, repeats, seed_base in (("validation", 1, 4017), ("test", 2, 6017)):
        for i, task in enumerate(t for t in tasks() if t.split == split):
            for repeat in range(repeats):
                order = POLICIES if pair % 2 == 0 else POLICIES[::-1]
                rows.extend(
                    {
                        "case_id": task.case_id,
                        "family": task.family,
                        "split": split,
                        "repeat": repeat,
                        "seed": seed_base + i * 10 + repeat,
                        "policy": policy,
                        "status": "not_run",
                    }
                    for policy in order
                )
                pair += 1
    return rows


def verify_sources(training: Path) -> None:
    """New analysis modules may be added; every source used in training stays identical."""
    expected = json.loads((training / "source.json").read_text())
    for path, digest in expected.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("training Harness/source changed before paired evaluation: " + path)
    summary = json.loads((training / "summary.json").read_text())
    if summary["completed_slots"] != summary["declared_slots"]:
        raise ValueError(
            "training queue incomplete: inspect its declared stop condition before evaluation"
        )


def _policy_paths(training: Path) -> dict[str, str]:
    base = json.loads(
        Path("benchmark-results/trainable_agent/core-protocol-v2/baseline-policy.json").read_text()
    )
    rl = json.loads((training / "final_weights.json").read_text())
    paths = {"base": base["model_path"], "rl": rl["path"]}
    if not all(isinstance(path, str) and path.startswith("trio://") for path in paths.values()):
        raise ValueError("invalid frozen policy URI")
    return paths


async def evaluate(output: Path, training: Path, budget_path: Path, image: str) -> None:
    """Sequential policy-paired runs; inference leases never overlap the training process."""
    await asyncio.to_thread(verify_sources, training)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    rows = evaluation_queue()
    for i, row in enumerate(rows):
        row["run_id"] = f"{output.name}_{i:03}_{row['policy']}"
    await asyncio.to_thread((output / "queue.json").write_text, json.dumps(rows, indent=2))
    paths = await asyncio.to_thread(_policy_paths, training)
    await asyncio.to_thread((output / "policies.json").write_text, json.dumps(paths, indent=2))
    task_map = {task.case_id: task for task in tasks()}
    with BudgetStore(budget_path, Decimal("50")) as budget:
        if budget.ledger.pending:
            raise ValueError("resolve pending provider operations before evaluation")
        service = trio.ServiceClient()
        samplers = {
            name: service.create_sampling_client(base_model="Qwen/Qwen3.5-4B", model_path=uri)
            for name, uri in paths.items()
        }
        tokenizers = {
            name: cast("Tokenizer", sampler.get_tokenizer()) for name, sampler in samplers.items()
        }
        expected = json.loads((training / "tokenizer.json").read_text())
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        for row in rows:
            name = str(row["policy"])
            backend = PyTrioBackend(
                samplers[name], tokenizers[name], budget, name, evaluation_reserve=Decimal("0")
            )
            identity = backend.identity()
            if any(identity[k] != expected[k] for k in ("template_hash", "vocabulary_hash")):
                raise ValueError("evaluation tokenizer differs from training")
            row["status"] = "running"
            await asyncio.to_thread((output / "queue.json").write_text, json.dumps(rows, indent=2))
            result = await run_rollout(
                task_map[str(row["case_id"])],
                backend,
                policy_id=name,
                run_id=str(row["run_id"]),
                seed=int(str(row["seed"])),
                output=output / str(row["run_id"]),
                host=host,
                image=image,
            )
            if budget.ledger.pending:
                raise ValueError("unknown provider usage; stop the evaluation queue")
            row.update(
                {
                    "status": result.status,
                    "success": result.success,
                    "tokens": result.total_tokens,
                    "tool_calls": result.tool_calls,
                    "duration_ms": result.duration_ms,
                    "failure_type": result.failure_type,
                }
            )
            await asyncio.to_thread((output / "queue.json").write_text, json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
        await asyncio.to_thread(
            (output / "completed.json").write_text,
            json.dumps(
                {
                    "declared": len(rows),
                    "completed": len(rows),
                    "conservative_spend_cny": str(budget.ledger.spent),
                },
                indent=2,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(
        evaluate(args.output.resolve(), args.training.resolve(), args.budget.resolve(), args.image)
    )


if __name__ == "__main__":
    main()

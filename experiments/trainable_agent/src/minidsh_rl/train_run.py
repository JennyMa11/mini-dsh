"""Sequential multi-turn GRPO controller; Smoke and core have separate LoRAs."""

import argparse
import asyncio
import hashlib
import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.budget import BudgetStore
from minidsh_rl.contracts import advantages
from minidsh_rl.datum import build_batch
from minidsh_rl.pytrio_backend import PyTrioBackend, Tokenizer
from minidsh_rl.rollout import run_rollout
from minidsh_rl.tasks import smoke_tasks, tasks
from minidsh_rl.trainer import Trainer
from minidsh_rl.transaction import UpdateJournal

ZERO_SIGNAL_LIMIT = 10
SMOKE_SLOT_COUNT = 2
TASK_GATE_COUNT = 80


def _source_snapshot() -> dict[str, str]:
    paths = [
        *Path("src/minidsh").rglob("*.py"),
        *Path("experiments/trainable_agent/src").rglob("*.py"),
        Path("uv.lock"),
        Path("pyproject.toml"),
        Path("experiments/trainable_agent/uv.lock"),
        Path("experiments/trainable_agent/pyproject.toml"),
    ]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def _require_core_gates() -> None:
    root = Path("benchmark-results/trainable_agent")
    for path, field in (
        (root / "training-probe-04/result.json", "mask_passed"),
        (root / "resume-probe-01/result.json", "passed"),
        (root / "security-gates-02/result.json", "passed"),
    ):
        if json.loads(path.read_text()).get(field) is not True:
            raise ValueError("required numerical/security gate has not passed")
    smoke = json.loads((root / "smoke-train-01/summary.json").read_text())
    if smoke["completed_slots"] != SMOKE_SLOT_COUNT or smoke["nonzero_updates"] < 1:
        raise ValueError("training Smoke has not completed with a real update")
    checks = json.loads((root / "task-gates-v3/summary.json").read_text())["checks"]
    if len(checks) != TASK_GATE_COUNT or any(row["passed"] != row["expected"] for row in checks):
        raise ValueError("independent task gates incomplete")


def _verify_tokenizer_identity(identity: dict[str, str]) -> None:
    expected = json.loads(
        Path("benchmark-results/trainable_agent/preflight/tokenizer.json").read_text()
    )
    if any(identity[k] != expected[k] for k in ("vocabulary_hash", "template_hash")):
        raise ValueError("tokenizer changed since preflight")


async def train(  # noqa: PLR0915 - sequential experiment protocol
    output: Path, budget_path: Path, image: str, *, phase: str
) -> None:
    """Freeze queue before model calls. Failed runs remain in their original groups."""
    if phase == "core":
        await asyncio.to_thread(_require_core_gates)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    if phase == "smoke":
        chosen = smoke_tasks()
        ordered = [chosen[1], chosen[3]]
    else:
        chosen = tuple(t for t in tasks() if t.split == "train")
        ordered = list(chosen)
        random.Random(17).shuffle(ordered)
        ordered += ordered[:4]
    queue: list[dict[str, object]] = [
        {"slot": i, "case_id": t.case_id, "status": "not_run"} for i, t in enumerate(ordered)
    ]
    config = {
        "schema_version": 1,
        "phase": phase,
        "base_model": "Qwen/Qwen3.5-4B",
        "seed": 17,
        "group_size": 4,
        "rank": 16,
        "train_unembed": False,
        "learning_rate": 1e-5,
        "slots": len(ordered),
        "reward": "success_v1",
        "loss": "ppo",
        "clip": [0.8, 1.2],
        "normalization": "assistant_token_mean_via_sample_mean_compensation",
    }
    await asyncio.to_thread((output / "config.json").write_text, json.dumps(config, indent=2))
    await asyncio.to_thread((output / "queue.json").write_text, json.dumps(queue, indent=2))
    sources = await asyncio.to_thread(_source_snapshot)
    await asyncio.to_thread((output / "source.json").write_text, json.dumps(sources, indent=2))
    with BudgetStore(budget_path, Decimal("50")) as budget:
        if budget.ledger.pending:
            raise ValueError("resolve existing unknown RPC before starting training")
        service = trio.ServiceClient()
        client = service.create_lora_training_client(
            base_model="Qwen/Qwen3.5-4B", rank=16, seed=17, train_unembed=False
        )
        await asyncio.to_thread(
            (output / "client.json").write_text,
            json.dumps({"training_run_cuid": client.training_run_cuid}, indent=2),
        )
        trainer = Trainer(client, budget, output, "sample_mean")
        sampler = await asyncio.to_thread(trainer.snapshot, "snapshot_000")
        policy_id = phase + "_policy_000"
        tokenizer = cast("Tokenizer", client.get_tokenizer())
        identity = PyTrioBackend(sampler, tokenizer, budget, policy_id).identity()
        await asyncio.to_thread(_verify_tokenizer_identity, identity)
        await asyncio.to_thread(
            (output / "tokenizer.json").write_text, json.dumps(identity, indent=2)
        )
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        zero_streak = 0
        for slot, task in enumerate(ordered):
            row = queue[slot]
            row["status"] = "running"
            await asyncio.to_thread((output / "queue.json").write_text, json.dumps(queue, indent=2))
            backend = PyTrioBackend(sampler, tokenizer, budget, policy_id)
            trajectories = []
            for repeat in range(4):
                run_id = f"{phase}_{slot:03}_{repeat}"
                result = await run_rollout(
                    task,
                    backend,
                    policy_id=policy_id,
                    run_id=run_id,
                    seed=(3000 if phase == "smoke" else 17) + slot * 100 + repeat,
                    output=output / run_id,
                    host=host,
                    image=image,
                )
                if budget.ledger.pending:
                    raise ValueError("unknown model RPC; group cannot be trained")
                trajectories.append(result)
            rewards = tuple(float(t.success) for t in trajectories)
            relative = advantages(rewards)
            group = {
                "slot": slot,
                "case_id": task.case_id,
                "policy_id": policy_id,
                "run_ids": [t.run_id for t in trajectories],
                "rewards": rewards,
                "advantages": relative,
            }
            await asyncio.to_thread(
                (output / f"group_{slot:03}.json").write_text, json.dumps(group, indent=2)
            )
            batch = build_batch(tuple(t.turns for t in trajectories), relative)
            update = await asyncio.to_thread(trainer.update, slot, batch)
            row.update(
                {
                    "status": "completed",
                    "rewards": rewards,
                    "updated": update["updated"],
                    "tokens": sum(t.total_tokens for t in trajectories),
                }
            )
            if update["updated"]:
                zero_streak = 0
                sampler = await asyncio.to_thread(trainer.snapshot, f"snapshot_{slot + 1:03}")
                policy_id = f"{phase}_policy_{slot + 1:03}"
            else:
                zero_streak += 1
            if phase == "smoke" or (slot + 1) % 10 == 0:
                checkpoint = await asyncio.to_thread(trainer.checkpoint, f"state_{slot + 1:03}")
                journal = UpdateJournal.load(output / f"update_{slot:03}.json")
                if row["updated"]:
                    await asyncio.to_thread(journal.advance, "checkpointed", checkpoint=checkpoint)
            await asyncio.to_thread((output / "queue.json").write_text, json.dumps(queue, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
            if zero_streak >= ZERO_SIGNAL_LIMIT:
                break
        final = await asyncio.to_thread(trainer.checkpoint, "final_state")
        weights = await asyncio.to_thread(
            trainer.charge,
            "final_weights",
            Decimal(".10"),
            lambda: client.save_weights_for_sampler("final_weights").result(),
        )
        await asyncio.to_thread(
            (output / "final_weights.json").write_text, weights.model_dump_json(indent=2)
        )
        summary = {
            "phase": phase,
            "declared_slots": len(ordered),
            "completed_slots": sum(r["status"] == "completed" for r in queue),
            "nonzero_updates": sum(r.get("updated") is True for r in queue),
            "final_state": final,
            "conservative_spend_cny": str(budget.ledger.spent),
        }
        await asyncio.to_thread((output / "summary.json").write_text, json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--phase", choices=("smoke", "core"), required=True)
    args = parser.parse_args()
    asyncio.run(train(args.output.resolve(), args.budget.resolve(), args.image, phase=args.phase))


if __name__ == "__main__":
    main()

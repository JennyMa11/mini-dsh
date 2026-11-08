"""Fresh Smoke/Pilot and checkpointed continuation on the frozen v2 task set."""

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import ROOT
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.contracts import advantages
from minidsh_rl.datum import build_batch
from minidsh_rl.harness_comparison import _require_gates, _snapshot
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.rollout_v2 import run_rollout
from minidsh_rl.tasks import Task, smoke_tasks
from minidsh_rl.tasks_v2 import expansion_tasks
from minidsh_rl.train_run import _require_core_gates, _verify_tokenizer_identity
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal

PILOT_SLOTS = 40
EXTENDED_SLOTS = 120
GROUP_BOUND = Decimal("1.13")
CLOSING = Decimal(".30")
SMOKE_SLOTS = 2
SCREEN_RUNS = 128
ZERO_LIMIT = 10
WALL_LIMIT = 4 * 3600


def training_schedule(phase: str) -> tuple[Task, ...]:
    """The first 40 groups are a prefix of the same fixed 120-group schedule."""
    if phase == "smoke":
        engineering = smoke_tasks()
        return engineering[1], engineering[3]
    if phase not in {"pilot", "extend"}:
        raise ValueError("unknown training phase")
    cases = [t for t in expansion_tasks() if t.split == "train"]
    random.Random(17).shuffle(cases)
    full = (cases * 3)[:EXTENDED_SLOTS]
    return tuple(full[:PILOT_SLOTS] if phase == "pilot" else full)


def _no_pending(path: Path) -> None:
    state = json.loads(path.read_text())
    if any(c["status"] == "pending" for c in state["charges"].values()):
        raise ValueError("unknown provider operation; stop and audit before further calls")


def _prepare(  # noqa: PLR0913, PLR0917, PLR0912 - phase gate boundary
    output: Path, image: str, phase: str, parent: Path | None, decision: Path | None, end: int
) -> tuple[list[dict[str, object]], dict[str, object]]:
    _require_core_gates()
    _require_gates(image)
    screening = ROOT / "expansion-v2/phase-a/screening-analysis-01.json"
    screen = json.loads(screening.read_text())
    if screen["ready_for_smoke"] is not True or screen["completed"] != SCREEN_RUNS:
        raise ValueError("full screening has not admitted training")
    for file, digest in screen["input_hashes"].items():
        if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest:
            raise ValueError("screening evidence changed")
    if phase != "smoke":
        smoke = json.loads(
            (ROOT / "expansion-v2/phase-a/smoke-train-v2-02/summary.json").read_text()
        )
        if smoke["completed_slots"] != SMOKE_SLOTS or smoke["nonzero_updates"] < 1:
            raise ValueError("new training Smoke needs a real nonzero update")
    start = 0
    resume_state = initial_uri = None
    if phase == "extend":
        if parent is None or decision is None:
            raise ValueError("extension requires confirmed parent and validation decision")
        summary_path = parent / "summary.json"
        previous = json.loads(summary_path.read_text())
        verdict = json.loads(decision.read_text())
        if (
            verdict["allow_extension"] is not True
            or verdict["allowed_end_slot"] != end
            or verdict["parent_summary_sha256"]
            != hashlib.sha256(summary_path.read_bytes()).hexdigest()
            or previous["completed_slots"] != previous["declared_slots"]
        ):
            raise ValueError("extension not admitted by completed parent/validation")
        start = previous["completed_until_slot"]
        resume_state, initial_uri = previous["final_state"], previous["initial_model_path"]
        if end not in (60, EXTENDED_SLOTS) or not start < end <= EXTENDED_SLOTS:
            raise ValueError("invalid extension interval")
    else:
        end = 2 if phase == "smoke" else PILOT_SLOTS
    schedule = training_schedule(phase)
    rows = [
        {"slot": index, "case_id": schedule[index].case_id, "status": "not_run"}
        for index in range(start, end)
    ]
    manifest = ROOT / "expansion-v2/phase-a/task-gates-01/manifest.json"
    config = {
        "phase": phase,
        "declared_slots": len(rows),
        "start_slot": start,
        "end_slot": end,
        "model": "Qwen/Qwen3.5-4B",
        "seed": 17,
        "group_size": 4,
        "rank": 16,
        "train_unembed": False,
        "learning_rate": 1e-5,
        "reward": "success_v1",
        "loss": "ppo",
        "clip": [0.8, 1.2],
        "normalization": "assistant_token_mean_via_sample_mean_compensation",
        "resume_state": resume_state,
        "initial_model_path": initial_uri,
        "instruction_suffix": "",
        "dataset_manifest_hash": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "screening_report_hash": hashlib.sha256(screening.read_bytes()).hexdigest(),
    }
    if parent is not None:
        parent_config = json.loads((parent / "config.json").read_text())
        if parent_config["dataset_manifest_hash"] != config["dataset_manifest_hash"]:
            raise ValueError("cannot resume a different task protocol")
        previous_order = json.loads((parent / "full_task_order.json").read_text())
        if previous_order != [t.case_id for t in training_schedule("extend")]:
            raise ValueError("training task order changed")
        for key in (
            "model",
            "seed",
            "group_size",
            "rank",
            "train_unembed",
            "learning_rate",
            "reward",
            "loss",
            "clip",
            "normalization",
        ):
            if parent_config[key] != config[key]:
                raise ValueError("training configuration changed across continuation")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.json").write_text(json.dumps(_snapshot(), indent=2))
    (output / "full_task_order.json").write_text(
        json.dumps([t.case_id for t in training_schedule("extend")], indent=2)
    )
    return rows, config


async def train(  # noqa: PLR0913, PLR0912, PLR0915 - sequential experiment protocol
    output: Path,
    image: str,
    *,
    phase: str,
    stage_cap: Decimal,
    parent: Path | None = None,
    decision: Path | None = None,
    end: int = 120,
) -> None:
    rows, config = await asyncio.to_thread(_prepare, output, image, phase, parent, decision, end)
    evaluation_reserve = Decimal("20") if phase == "smoke" else Decimal("5.12")
    local_eval = Decimal("0") if phase == "smoke" else Decimal("5.12")
    if not stage_cap.is_finite() or stage_cap < GROUP_BOUND + CLOSING + local_eval:
        raise ValueError("stage allocation cannot fit a full group and evaluation")
    config.update(
        {"stage_cap_cny": str(stage_cap), "evaluation_reserve_cny": str(evaluation_reserve)}
    )
    await asyncio.to_thread((output / "config.json").write_text, json.dumps(config, indent=2))
    queue = output / "queue.json"
    await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
    audit_path = ROOT / "expansion-v2/reconciliation-01/audit.json"
    with CalibratedBudget(ROOT / "expansion-v2/calibrated-budget.json", audit=audit_path) as budget:
        await asyncio.to_thread(_no_pending, budget.path)
        baseline = budget.committed
        if phase == "smoke":
            screening_summary = json.loads(
                await asyncio.to_thread(
                    (ROOT / "expansion-v2/phase-a/screening-01/summary.json").read_text
                )
            )
            if Decimal(screening_summary["phase_a_sampling_upper_cny"]) + stage_cap > Decimal("8"):
                raise ValueError("Smoke stage allocation does not fit phase A cap")
        if budget.available < stage_cap:
            raise ValueError("entire stage allocation does not fit global budget")
        service = trio.ServiceClient()
        operation = output.name + "_create_client"
        await asyncio.to_thread(budget.reserve, operation, Decimal(".01"))
        if config["resume_state"]:
            client = await asyncio.to_thread(
                service.create_training_client_from_state_with_optimizer,
                str(config["resume_state"]),
            )
        else:
            client = await asyncio.to_thread(
                service.create_lora_training_client,
                base_model="Qwen/Qwen3.5-4B",
                rank=16,
                seed=17,
                train_unembed=False,
            )
        await asyncio.to_thread(budget.settle, operation, Decimal(".01"))
        await asyncio.to_thread(
            (output / "client.json").write_text,
            json.dumps({"training_run_cuid": client.training_run_cuid}, indent=2),
        )
        trainer = TrainerV2(client, budget, output, "sample_mean", evaluation_reserve)
        if config["initial_model_path"]:
            initial_uri = str(config["initial_model_path"])
            sampler = await asyncio.to_thread(trainer.snapshot, "resumed_snapshot")
        else:
            initial = await asyncio.to_thread(
                trainer.charge,
                "initial_weights",
                Decimal(".10"),
                lambda: client.save_weights_for_sampler("initial_weights").result(),
            )
            initial_uri = str(initial.path)
            await asyncio.to_thread(
                (output / "initial_weights.json").write_text, initial.model_dump_json(indent=2)
            )
            sampler = service.create_sampling_client(
                base_model="Qwen/Qwen3.5-4B", model_path=initial_uri
            )
        if not initial_uri.startswith("trio://"):
            raise ValueError("invalid initial sampler checkpoint")
        tokenizer = cast(Tokenizer, client.get_tokenizer())
        policy_id = f"{output.name}_policy_{config['start_slot']}"
        identity = PricedBackend(sampler, tokenizer, budget, policy_id).identity()
        await asyncio.to_thread(_verify_tokenizer_identity, identity)
        await asyncio.to_thread(
            (output / "tokenizer.json").write_text, json.dumps(identity, indent=2)
        )
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        schedule = training_schedule(phase)
        zero_streak = 0
        stopped = None
        began = time.monotonic()
        for row in rows:
            if (
                budget.committed - baseline + GROUP_BOUND + CLOSING + local_eval > stage_cap
                or budget.available < GROUP_BOUND + CLOSING + evaluation_reserve
            ):
                stopped = "resource_admission_exhausted"
                break
            if time.monotonic() - began > WALL_LIMIT:
                stopped = "stage_wall_clock_exhausted"
                break
            slot = int(str(row["slot"]))
            task = schedule[slot]
            row["status"] = "running"
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            backend = PricedBackend(
                sampler,
                tokenizer,
                budget,
                policy_id,
                evaluation_reserve=evaluation_reserve + CLOSING,
            )
            trajectories = []
            for repeat in range(4):
                run_id = f"{output.name}_{slot:03}_{repeat}"
                result = await run_rollout(
                    task,
                    backend,
                    policy_id=policy_id,
                    run_id=run_id,
                    seed=(3000 if phase == "smoke" else 17) + slot * 100 + repeat,
                    output=output / run_id,
                    host=host,
                    image=image,
                    instruction_suffix="",
                )
                await asyncio.to_thread(_no_pending, budget.path)
                trajectories.append(result)
            rewards = tuple(float(t.success) for t in trajectories)
            relative = advantages(rewards)
            await asyncio.to_thread(
                (output / f"group_{slot:03}.json").write_text,
                json.dumps(
                    {
                        "slot": slot,
                        "case_id": task.case_id,
                        "policy_id": policy_id,
                        "run_ids": [t.run_id for t in trajectories],
                        "rewards": rewards,
                        "advantages": relative,
                    },
                    indent=2,
                ),
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
                policy_id = f"{output.name}_policy_{slot + 1:03}"
            else:
                zero_streak += 1
            if phase == "smoke" or (slot + 1) % 10 == 0:
                checkpoint = await asyncio.to_thread(trainer.checkpoint, f"state_{slot + 1:03}")
                if row["updated"]:
                    journal = UpdateJournal.load(output / f"update_{slot:03}.json")
                    await asyncio.to_thread(journal.advance, "checkpointed", checkpoint=checkpoint)
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
            if zero_streak >= ZERO_LIMIT:
                stopped = "ten_consecutive_zero_signal_groups"
                break
        final_state = await asyncio.to_thread(trainer.checkpoint, "final_state")
        weights = await asyncio.to_thread(
            trainer.charge,
            "final_weights",
            Decimal(".10"),
            lambda: client.save_weights_for_sampler("final_weights").result(),
        )
        await asyncio.to_thread(
            (output / "final_weights.json").write_text, weights.model_dump_json(indent=2)
        )
        completed = sum(r["status"] == "completed" for r in rows)
        summary = {
            "phase": phase,
            "declared_slots": len(rows),
            "completed_slots": completed,
            "completed_until_slot": int(str(config["start_slot"])) + completed,
            "nonzero_updates": sum(r.get("updated") is True for r in rows),
            "stop_reason": stopped,
            "initial_model_path": initial_uri,
            "final_state": final_state,
            "training_seed": 17,
            "global_committed_upper_cny": str(budget.committed),
            "stage_spend_upper_cny": str(budget.committed - baseline),
            "stage_cap_cny": str(stage_cap),
            "reserved_local_evaluation_cny": str(local_eval),
        }
        await asyncio.to_thread((output / "summary.json").write_text, json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--phase", choices=("smoke", "pilot", "extend"), required=True)
    parser.add_argument("--stage-cap-cny", type=Decimal, required=True)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--end-slot", type=int, choices=(60, 120), default=120)
    args = parser.parse_args()
    asyncio.run(
        train(
            args.output,
            args.image,
            phase=args.phase,
            stage_cap=args.stage_cap_cny,
            parent=args.parent,
            decision=args.decision,
            end=args.end_slot,
        )
    )


if __name__ == "__main__":
    main()

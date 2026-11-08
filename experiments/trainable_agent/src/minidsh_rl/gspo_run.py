"""Two-group engineering Smoke of the Lab GSPO multi-turn adaptation."""

import argparse
import asyncio
import hashlib
import json
import tarfile
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import ROOT
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.contracts import advantages
from minidsh_rl.gspo import GSPOConfig, build_gspo_batch
from minidsh_rl.gspo_numeric import verify_remote_reduction
from minidsh_rl.gspo_trainer import GSPOTrainer
from minidsh_rl.harness_comparison import _require_gates, _snapshot
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.rollout_v2 import run_rollout
from minidsh_rl.tasks import smoke_tasks
from minidsh_rl.tracking import MetricRecorder, RunSpec
from minidsh_rl.train_run import _require_core_gates, _verify_tokenizer_identity
from minidsh_rl.train_v2 import _no_pending
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal
from minidsh_rl.weight_probe import weight_delta

STAGE_CAP = Decimal("5")
GROUP_BOUND = Decimal("1.70")
CLOSING = Decimal(".30")
NUMERIC_BOUND = Decimal("1")
WALL_LIMIT = 1800


def run(  # noqa: PLR0915 - sequential engineering protocol
    output: Path, image: str, mode: Literal["offline", "online"]
) -> None:
    """A fixed queue, shared ¥50 ledger, no automatic resume or test-set access."""
    trio.configure(request_timeout_seconds=600, sampling_timeout_seconds=300)
    _require_core_gates()
    _require_gates(image)
    output.mkdir(parents=True, exist_ok=False)
    config = GSPOConfig(0.2, 0.2)
    protocol = {
        "schema_version": 1,
        "phase": "engineering_smoke",
        "not_efficacy_evaluation": True,
        "lab_revision": "238bc688ca7d5c994c580f732ee9c2c92483bd20",
        "algorithm": "gspo_multiturn_wide_clip_v1",
        "config": asdict(config),
        "advantage": "group_centered_no_std",
        "objective": "equal_trajectory_mean",
        "group_size": 4,
        "groups": 2,
        "seed": 17,
        "image": image,
        "numeric_upper_cny": str(NUMERIC_BOUND),
        "stage_upper_cny": str(STAGE_CAP),
        "global_limit_cny": "50",
        "training_compute_reservation_factor": 2,
        "cross_entropy_reduction": "sum",
        "migration_start_upper_cny": "41.163",
        "fresh_policy_active_clip_fraction_max": 0.05,
        "wall_limit_seconds": 1800,
        "tasks": [smoke_tasks()[i].case_id for i in (1, 3)],
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2))
    source = _snapshot()
    (output / "source.json").write_text(json.dumps(source, indent=2))
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for file in sorted(source):
            archive.add(file, arcname=file, recursive=False)
    recorder = MetricRecorder(
        output / "tracking",
        RunSpec(
            output.name,
            "smoke",
            "gspo_multiturn",
            hashlib.sha256((output / "source.json").read_bytes()).hexdigest(),
        ),
    )
    recorder.connect(mode=mode)
    ledger = ROOT / "expansion-v2/calibrated-budget.json"
    audit = ROOT / "expansion-v2/reconciliation-01/audit.json"
    began = time.monotonic()
    with CalibratedBudget(ledger, audit=audit) as budget:
        _no_pending(ledger)
        attempt_start = budget.committed
        start = Decimal("41.163")
        if budget.committed < start:
            raise ValueError("historical migration baseline missing")
        remaining = STAGE_CAP - (attempt_start - start)
        if budget.available < remaining or remaining < NUMERIC_BOUND + GROUP_BOUND + CLOSING:
            raise ValueError("stage cannot fit remaining cumulative budget")
        numeric = verify_remote_reduction(output / (output.name + "-numeric"), budget)
        if budget.committed - attempt_start > NUMERIC_BOUND:
            raise ValueError("numeric cost gate exceeded; do not start training")
        recorder.log(
            0,
            {
                "numeric/mask_passed": float(numeric["mask_passed"] is True),
                "numeric/remote_gradient_verified": float(numeric["passed"] is True),
            },
        )
        service = trio.ServiceClient()
        operation = (
            output.name
            + "_client_"
            + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16]
        )
        budget.reserve(operation, Decimal(".01"))
        client = service.create_lora_training_client(
            base_model="Qwen/Qwen3.5-4B", rank=16, seed=17, train_unembed=False
        )
        budget.settle(operation, Decimal(".01"))
        base = TrainerV2(client, budget, output, "sum", CLOSING)
        trainer = GSPOTrainer(base, config)
        rest = service.create_rest_client()

        def export(name: str) -> tuple[str, Path]:
            saved = base.charge(
                name, Decimal(".10"), lambda: client.save_weights_for_sampler(name).result()
            )
            (output / (name + ".json")).write_text(saved.model_dump_json(indent=2))
            checkpoints = rest.list_checkpoints(client.training_run_cuid).result()
            cid = next(c.checkpoint_id for c in checkpoints.checkpoints if c.path == saved.path)
            path = output / (name + ".archive")
            rest.download_checkpoint(cid, path, resume=False).result()
            return str(saved.path), path

        initial_uri, initial_archive = export("initial_weights")
        sampler = service.create_sampling_client(
            base_model="Qwen/Qwen3.5-4B", model_path=initial_uri
        )
        tokenizer = cast(Tokenizer, client.get_tokenizer())
        _verify_tokenizer_identity(PricedBackend(sampler, tokenizer, budget, "initial").identity())
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        rows: list[dict[str, object]] = [
            {"slot": slot, "case_id": smoke_tasks()[i].case_id, "status": "not_run"}
            for slot, i in enumerate((1, 3))
        ]
        queue = output / "queue.json"
        queue.write_text(json.dumps(rows, indent=2))
        total_inference_tokens = 0
        for slot, index in enumerate((1, 3)):
            if budget.committed - start + GROUP_BOUND + CLOSING > STAGE_CAP:
                raise ValueError("full next group cannot fit stage allocation")
            if time.monotonic() - began > WALL_LIMIT:
                raise TimeoutError("stage wall clock gate reached")
            policy = f"{output.name}_policy_{slot}"
            backend = PricedBackend(sampler, tokenizer, budget, policy, evaluation_reserve=CLOSING)
            results = []
            for repeat in range(4):
                run_id = f"{output.name}_{slot:03}_{repeat}"
                result = asyncio.run(
                    run_rollout(
                        smoke_tasks()[index],
                        backend,
                        policy_id=policy,
                        run_id=run_id,
                        seed=3000 + slot * 100 + repeat,
                        output=output / run_id,
                        host=host,
                        image=image,
                        instruction_suffix="",
                    )
                )
                _no_pending(ledger)
                results.append(result)
            total_inference_tokens += sum(t.total_tokens for t in results)
            rewards = tuple(float(t.success) for t in results)
            relative = advantages(rewards)
            batch = build_gspo_batch(tuple(t.turns for t in results), relative)
            (output / f"group_{slot:03}.json").write_text(
                json.dumps(
                    {
                        "slot": slot,
                        "case_id": smoke_tasks()[index].case_id,
                        "policy_id": policy,
                        "run_ids": [t.run_id for t in results],
                        "rewards": rewards,
                        "advantages": relative,
                        "turn_counts": [len(t.turns) for t in results],
                    },
                    indent=2,
                )
            )
            update = trainer.update(slot, batch)
            rows[slot].update(
                {"status": "completed", "rewards": rewards, "updated": update["updated"]}
            )
            if update["updated"]:
                sampler = base.snapshot(f"snapshot_{slot + 1:03}")
                state = base.checkpoint(f"state_{slot + 1:03}")
                UpdateJournal.load(output / f"update_{slot:03}.json").advance(
                    "checkpointed", checkpoint=state
                )
            queue.write_text(json.dumps(rows, indent=2))
            metrics = dict(cast(dict[str, float], update["metrics"]))
            metrics.update(
                {
                    "reward/mean": sum(rewards) / 4,
                    "rollout/completed": float((slot + 1) * 4),
                    "rollout/declared": 8.0,
                    "rollout/zero_signal": float(not update["updated"]),
                    "usage/group_tokens": float(sum(t.total_tokens for t in results)),
                    "budget/stage_upper_cny": float(budget.committed - start),
                    "budget/cumulative_upper_cny": float(budget.committed),
                    "train/updates": float(sum(r.get("updated") is True for r in rows)),
                }
            )
            recorder.log(slot + 1, metrics)
        final_state = base.checkpoint("final_state")
        final_uri, final_archive = export("final_weights")
        delta = weight_delta(initial_archive, final_archive)
        summary = {
            "schema_version": 1,
            "declared_rollouts": 8,
            "completed_rollouts": 4 * sum(r["status"] == "completed" for r in rows),
            "nonzero_updates": sum(r.get("updated") is True for r in rows),
            "adapter_weight_delta": delta,
            "numeric_probe_passed": numeric["passed"],
            "stage_upper_cny": str(budget.committed - start),
            "attempt_upper_cny": str(budget.committed - attempt_start),
            "cumulative_upper_cny": str(budget.committed),
            "actual_provider_bill_cny": None,
            "initial_uri": initial_uri,
            "final_uri": final_uri,
            "final_state": final_state,
            "source_unchanged": _snapshot() == source,
            "efficacy_evaluation": False,
            "elapsed_seconds": time.monotonic() - began,
        }
        summary["passed"] = bool(
            summary["numeric_probe_passed"]
            and summary["nonzero_updates"]
            and delta > 0
            and summary["source_unchanged"]
            and budget.committed - start <= STAGE_CAP
        )
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        _no_pending(ledger)
        recorder.log(
            3,
            {
                "budget/stage_upper_cny": float(budget.committed - start),
                "budget/cumulative_upper_cny": float(budget.committed),
                "usage/stage_total_tokens": float(total_inference_tokens),
            },
        )
    recorder.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--swanlab-mode", choices=("offline", "online"), default="offline")
    args = parser.parse_args()
    run(args.output, args.image, args.swanlab_mode)


if __name__ == "__main__":
    main()

"""128 Base rollouts on new train/validation tasks; never sample the new test set."""

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
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import ROOT, price_card
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.harness_comparison import _require_gates, _snapshot
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.rollout_v2 import run_rollout
from minidsh_rl.tasks_v2 import expansion_tasks

PHASE_CAP = Decimal("8")
SMOKE_RESERVE = Decimal("1.5")
PILOT_RESERVE = Decimal("20")
RUN_BOUND = Decimal(".08")


def screening_queue() -> list[dict[str, object]]:
    """Fixed order independent of outcomes; same two repeats for all 64 eligible cases."""
    cases = [t for t in expansion_tasks() if t.split in {"train", "validation"}]
    random.Random(37).shuffle(cases)
    return [
        {
            "case_id": t.case_id,
            "family": t.family,
            "split": t.split,
            "repeat": repeat,
            "seed": 15017 + index * 10 + repeat,
            "status": "not_run",
        }
        for index, t in enumerate(cases)
        for repeat in range(2)
    ]


def _prepare(output: Path, image: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    _require_gates(image)
    selection_path = ROOT / "expansion-v2/phase-a/harness-selection.json"
    selection = json.loads(selection_path.read_text())
    if selection["selected"] != "original" or selection["instruction_suffix"] != "":
        raise ValueError("screening protocol expects the predeclared original selection")
    analysis = ROOT / "expansion-v2/phase-a/harness-analysis-01.json"
    manifest = ROOT / "expansion-v2/phase-a/task-gates-01/manifest.json"
    for p, key in ((analysis, "analysis_sha256"), (manifest, "task_manifest_sha256")):
        if hashlib.sha256(p.read_bytes()).hexdigest() != selection[key]:
            raise ValueError("selection evidence changed")
    output.mkdir(parents=True, exist_ok=False)
    rows = screening_queue()
    for index, row in enumerate(rows):
        row["run_id"] = f"{output.name}_{index:03}"
    protocol = {
        "declared": len(rows),
        "new_test_model_calls": 0,
        "training": False,
        "order_seed": 37,
        "instruction_suffix": "",
        "sources": _snapshot(),
        "image": image,
        "phase_a_cap_cny": str(PHASE_CAP),
        "smoke_reserve_cny": str(SMOKE_RESERVE),
        "pilot_reserve_cny": str(PILOT_RESERVE),
        "eligible_split": ["train", "validation"],
        "task_gate_manifest_hash": selection["task_manifest_sha256"],
        "model": "Qwen/Qwen3.5-4B",
        "group_selection": "none; no reward-based resampling",
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2))
    return rows, protocol


async def screen(output: Path, image: str) -> None:
    """Record every declaration; admission protects future Smoke and Pilot allocations."""
    audit_path = ROOT / "expansion-v2/reconciliation-01/audit.json"
    audit = json.loads(await asyncio.to_thread(audit_path.read_text))
    quote = Path(audit["quote"])
    await asyncio.to_thread(price_card, quote)
    rows, _ = await asyncio.to_thread(_prepare, output, image)
    queue = output / "queue.json"
    await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
    prior_a = (
        Decimal(audit["cohorts"]["development"]["new_sampling_estimate"])
        + Decimal(audit["calibration"]["new"])
        + Decimal(".01")
    )
    tasks = {t.case_id: t for t in expansion_tasks()}
    with CalibratedBudget(ROOT / "expansion-v2/calibrated-budget.json", audit=audit_path) as budget:
        state = json.loads(await asyncio.to_thread(budget.path.read_text))
        if any(c["status"] == "pending" for c in state["charges"].values()):
            raise ValueError("pending operations require audit; do not restart sampler")
        base = Decimal(audit["carried_cny"]) + Decimal(audit["uncertainty_cny"])
        service = trio.ServiceClient()
        sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B")
        tokenizer = cast(Tokenizer, sampler.get_tokenizer())
        backend = PricedBackend(
            sampler, tokenizer, budget, "base_screen_v2", evaluation_reserve=PILOT_RESERVE
        )
        await asyncio.to_thread(
            (output / "policy.json").write_text,
            json.dumps(
                backend.identity() | {"sampling_run_cuid": sampler.sampling_run_cuid}, indent=2
            ),
        )
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        completed = 0
        stop_reason = None
        for row in rows:
            phase_spend = prior_a + budget.committed - base
            if (
                phase_spend + RUN_BOUND + SMOKE_RESERVE > PHASE_CAP
                or budget.available < RUN_BOUND + PILOT_RESERVE
            ):
                stop_reason = "resource_admission_exhausted"
                break
            row["status"] = "running"
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            result = await run_rollout(
                tasks[str(row["case_id"])],
                backend,
                policy_id=backend.policy_id,
                run_id=str(row["run_id"]),
                seed=int(str(row["seed"])),
                output=output / str(row["run_id"]),
                host=host,
                image=image,
                instruction_suffix="",
            )
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
            completed += 1
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
        await asyncio.to_thread(
            (output / "summary.json").write_text,
            json.dumps(
                {
                    "declared": len(rows),
                    "completed": completed,
                    "stop_reason": stop_reason,
                    "global_committed_upper_cny": str(budget.committed),
                    "new_test_model_calls": 0,
                    "phase_a_sampling_upper_cny": str(prior_a + budget.committed - base),
                    "training": False,
                },
                indent=2,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(screen(args.output, args.image))


if __name__ == "__main__":
    main()

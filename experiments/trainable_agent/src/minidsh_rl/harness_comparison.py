"""48 fixed development rollouts: only the JSON-newline instruction changes."""

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
from minidsh_rl.backend_v2 import ExpansionBackend, Tokenizer
from minidsh_rl.billing_calibration import ROOT, price_card
from minidsh_rl.cost_store import CostStore
from minidsh_rl.rollout_v2 import run_rollout
from minidsh_rl.tasks import tasks

SUFFIX = (
    " Editing guidance: avoid triple-quoted source strings inside argv. "
    "JSON strings must not contain literal line breaks. Encode source newlines as "
    r"\n inside the JSON string. Prefer a short p.read_text().replace edit when possible."
)
PHASE_LIMIT = Decimal("8")
UNCERTAINTY = Decimal("2")


def development_queue() -> list[dict[str, object]]:
    """Exactly 24 old cases; the new held-out test set is never used here."""
    selected = [
        t for t in tasks() if t.split == "train" or t.family in {"pagination", "deduplicate"}
    ]
    rows: list[dict[str, object]] = []
    for index, task in enumerate(selected):
        variants = (
            ("original", "json_newlines") if index % 2 == 0 else ("json_newlines", "original")
        )
        rows.extend(
            {
                "case_id": task.case_id,
                "variant": variant,
                "seed": 12017 + index,
                "status": "not_run",
                "run_id": f"development_{index:02}_{variant}",
            }
            for variant in variants
        )
    return rows


def _snapshot() -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            *Path("experiments/trainable_agent/src").rglob("*.py"),
            *Path("src/minidsh").rglob("*.py"),
            Path("uv.lock"),
            Path("experiments/trainable_agent/uv.lock"),
            Path("pyproject.toml"),
            Path("experiments/trainable_agent/pyproject.toml"),
        ]
    }


def _require_gates(image: str) -> None:
    gate_root = ROOT / "expansion-v2/phase-a/task-gates-01"
    gate = json.loads((gate_root / "summary.json").read_text())
    if (
        gate["passed"] is not True
        or gate["completed"] != gate["declared"]
        or gate["image"] != image
    ):
        raise ValueError("new-task component gates incomplete")
    for file, digest in json.loads((gate_root / "sources.json").read_text()).items():
        if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest:
            raise ValueError("component gate source changed")


async def compare(output: Path, quote: Path, image: str) -> None:
    """Freeze pair order; require worst-case whole block admission, never hidden retries."""
    await asyncio.to_thread(price_card, quote)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    await asyncio.to_thread(_require_gates, image)
    rows = development_queue()
    for row in rows:
        row["run_id"] = output.name + "_" + str(row["run_id"])
    queue = output / "queue.json"
    await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
    source = await asyncio.to_thread(_snapshot)
    await asyncio.to_thread(
        (output / "protocol.json").write_text,
        json.dumps(
            {
                "declared": len(rows),
                "scope": "old_v1_tasks_used_for_development_only",
                "model": "Qwen/Qwen3.5-4B",
                "training": False,
                "single_variable": "instruction_suffix",
                "instruction_suffix": SUFFIX,
                "sources": source,
                "max_model_calls_per_run": 8,
                "phase_a_cap_cny": str(PHASE_LIMIT),
                "new_test_model_calls": 0,
                "selection_rule": "adopt suffix only if development success count does not fall "
                "and rejected/model_call rate drops at least 30%; otherwise retain original",
                "zero_rejections_rule": "retain original if both have no rejected actions",
            },
            indent=2,
        ),
    )
    with CostStore(
        ROOT / "expansion-v2/budget.json",
        legacy=ROOT / "core-budget.json",
        quote=quote,
        limit=Decimal("50"),
        uncertainty=UNCERTAINTY,
    ) as budget:
        state = json.loads((ROOT / "expansion-v2/budget.json").read_text())
        if any(c["status"] == "pending" for c in state["charges"].values()):
            raise ValueError("unknown provider operations must be audited first")
        phase_spend = budget.committed - Decimal(state["historical"]) - UNCERTAINTY
        maximum = len(rows) * 8 * Decimal(".02")
        if phase_spend + maximum > PHASE_LIMIT or budget.available < maximum:
            raise ValueError("complete development block cannot fit phase/global budget")
        service = trio.ServiceClient()
        sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B")
        tokenizer = cast(Tokenizer, sampler.get_tokenizer())
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        task_map = {t.case_id: t for t in tasks()}
        for row in rows:
            variant = str(row["variant"])
            backend = ExpansionBackend(sampler, tokenizer, budget, "base_" + variant)
            if not (output / "tokenizer.json").exists():
                await asyncio.to_thread(
                    (output / "tokenizer.json").write_text,
                    json.dumps(
                        backend.identity() | {"sampling_run_cuid": sampler.sampling_run_cuid},
                        indent=2,
                    ),
                )
            row["status"] = "running"
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            result = await run_rollout(
                task_map[str(row["case_id"])],
                backend,
                policy_id=backend.policy_id,
                run_id=str(row["run_id"]),
                seed=int(str(row["seed"])),
                output=output / str(row["run_id"]),
                host=host,
                image=image,
                instruction_suffix=SUFFIX if variant == "json_newlines" else "",
            )
            row.update(
                {
                    "status": result.status,
                    "success": result.success,
                    "tokens": result.total_tokens,
                    "tool_calls": result.tool_calls,
                    "duration_ms": result.duration_ms,
                }
            )
            await asyncio.to_thread(queue.write_text, json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
        await asyncio.to_thread(
            (output / "summary.json").write_text,
            json.dumps(
                {
                    "declared": len(rows),
                    "completed": len(rows),
                    "global_committed_upper_cny": str(budget.committed),
                    "training": False,
                    "new_test_model_calls": 0,
                },
                indent=2,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quote", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(compare(args.output, args.quote, args.image))


if __name__ == "__main__":
    main()

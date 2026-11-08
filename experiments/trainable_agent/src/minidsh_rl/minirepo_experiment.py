"""Frozen 32-run Base interaction diagnostic; shared cumulative ¥50 accounting."""

import argparse
import asyncio
import hashlib
import json
import tarfile
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import ROOT, price_card
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.harness_comparison import _snapshot
from minidsh_rl.minirepo_prompts import TOOL_EXAMPLE, PromptVariant, instruction
from minidsh_rl.minirepo_rollout import run_rollout
from minidsh_rl.minirepo_studies import BATCH_LIMIT, BATCH_READ, PROMPT, WORKFLOW
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import ReadVariant
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.tracking import MetricRecorder, RunSpec
from minidsh_rl.train_run import _verify_tokenizer_identity
from minidsh_rl.train_v2 import _no_pending

RUN_BOUND = Decimal(".09")
STAGE_CAP = Decimal("3")
GATE_CASES = 56
PROMPT_STUDY_OUTPUT_CAP = 1024
PROMPT_VARIANTS: tuple[PromptVariant, ...] = ("original", "tool_example")


def run_cost_bound() -> Decimal:
    """Worst all-token quote plus <.001/category/call rounding; cache ignored."""
    return (Decimal(36000) * Decimal("1.49") + Decimal(3072) * Decimal("3.05")) / 1000000 + Decimal(
        ".024"
    )


def evaluation_queue() -> list[dict[str, object]]:
    rows = []
    pair = 0
    for i, task in enumerate(development_tasks()):
        for repeat in range(2):
            order = ("single", "interactive") if pair % 2 == 0 else ("interactive", "single")
            for mode in order:
                rows.append(  # noqa: PERF401 - retain explicit alternating pair order
                    {
                        "case_id": task.case_id,
                        "family": task.family,
                        "mode": mode,
                        "seed": 54017 + i * 10 + repeat,
                        "repeat": repeat,
                        "status": "not_run",
                    }
                )
            pair += 1
    return rows


def prompt_evaluation_queue() -> list[dict[str, object]]:
    """Keep the complete paired task/seed schedule; both arms allow revision."""
    return [
        row
        | {
            "mode": "interactive",
            "prompt_variant": "original" if row["mode"] == "single" else "tool_example",
        }
        for row in evaluation_queue()
    ]


def batch_evaluation_queue() -> list[dict[str, object]]:
    """Compare optional batch capability with the same original instruction."""
    return [
        row
        | {
            "mode": "interactive",
            "prompt_variant": "original",
            "read_variant": "single_read" if row["mode"] == "single" else "batch_read",
        }
        for row in evaluation_queue()
    ]


def limit_evaluation_queue() -> list[dict[str, object]]:
    """Keep both arms interactive with the same batch capability and instruction."""
    return [
        row
        | {
            "read_variant": "batch_read"
            if row["read_variant"] == "single_read"
            else "batch_read_eight"
        }
        for row in batch_evaluation_queue()
    ]


async def evaluate(  # noqa: PLR0912, PLR0913, PLR0915 - frozen sequential protocol
    output: Path,
    gates: Path,
    quote: Path,
    *,
    mode: Literal["online", "offline"],
    max_completion_per_call: int = 512,
    prompt_study: bool = False,
    prompt_control: Path | None = None,
    batch_study: bool = False,
    batch_control: Path | None = None,
    limit_study: bool = False,
) -> None:
    if sum((prompt_study, batch_study, limit_study)) > 1:
        raise ValueError("only one comparison variable per study")
    if (
        prompt_study or batch_study or limit_study
    ) and max_completion_per_call != PROMPT_STUDY_OUTPUT_CAP:
        raise ValueError("development study requires the frozen 1024 output cap")
    layout = (
        BATCH_LIMIT
        if limit_study
        else BATCH_READ
        if batch_study
        else PROMPT
        if prompt_study
        else WORKFLOW
    )
    gate = json.loads(await asyncio.to_thread(gates.read_text))
    if not gate["passed"] or gate["completed"] != GATE_CASES or gate["image"] != IMAGE:
        raise ValueError("mini-repository offline gates incomplete")
    for name, digest in gate["source_sha256"].items():
        if hashlib.sha256(await asyncio.to_thread(Path(name).read_bytes)).hexdigest() != digest:
            raise ValueError("offline gate source changed")
    if prompt_study:
        if prompt_control is None:
            raise ValueError("tool-example control probe required")
        control_bytes = await asyncio.to_thread(prompt_control.read_bytes)
        control = json.loads(control_bytes)
        if (
            not control["passed"]
            or control["new_model_calls"] != 0
            or control["example_sha256"] != hashlib.sha256(TOOL_EXAMPLE.encode()).hexdigest()
        ):
            raise ValueError("tool-example control probe incomplete")
        for name, digest in control["source_sha256"].items():
            if hashlib.sha256(await asyncio.to_thread(Path(name).read_bytes)).hexdigest() != digest:
                raise ValueError("control probe source changed")
    if batch_study or limit_study:
        if batch_control is None:
            raise ValueError("batch-read control probe required")
        batch_bytes = await asyncio.to_thread(batch_control.read_bytes)
        batch = json.loads(batch_bytes)
        if not batch["passed"] or batch["new_model_calls"] != 0 or not batch["same_observed_files"]:
            raise ValueError("batch-read control probe incomplete")
        if limit_study and batch.get("path_limits") != {"batch_read": 4, "batch_read_eight": 8}:
            raise ValueError("batch-limit control probe incomplete")
        for name, digest in batch["source_sha256"].items():
            if hashlib.sha256(await asyncio.to_thread(Path(name).read_bytes)).hexdigest() != digest:
                raise ValueError("batch control source changed")
    price_card(quote)
    if run_cost_bound() >= RUN_BOUND:
        raise ValueError("invalid monetary bound")
    rows = (
        limit_evaluation_queue()
        if limit_study
        else batch_evaluation_queue()
        if batch_study
        else prompt_evaluation_queue()
        if prompt_study
        else evaluation_queue()
    )
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    for i, row in enumerate(rows):
        label = row[layout.comparison_key]
        row["run_id"] = f"{output.name}_{i:03}_{label}"
    queue = output / "queue.json"
    queue.write_text(json.dumps(rows, indent=2))
    policy_source = ROOT / "expansion-v2/pilot-train-01/initial_weights.json"
    uri = str(json.loads(policy_source.read_text())["path"])
    if not uri.startswith("trio://"):
        raise ValueError("missing persistent initial policy")
    protocol: dict[str, object] = {
        "schema_version": 1,
        "phase": layout.phase,
        "comparison_key": layout.comparison_key,
        "declared": 32,
        "model": "Qwen/Qwen3.5-4B",
        "initial_policy_source_sha256": hashlib.sha256(policy_source.read_bytes()).hexdigest(),
        "grouping": layout.grouping,
        "policy": "initial_zero_lora_rank16_seed17",
        "reward": "independent_success_only",
        "training_updates": 0,
        "image": IMAGE,
        "stage_cap_cny": str(STAGE_CAP),
        "reserved_evaluation_cny": str(len(rows) * RUN_BOUND),
        "single_run_bound_cny": str(RUN_BOUND),
        "bound_proof_cny": str(run_cost_bound()),
        "max_steps": 12,
        "max_tool_calls": 12,
        "max_tokens": 36000,
        "max_output_tokens": 3072,
        "context_limit": 6144,
        "max_completion_per_call": max_completion_per_call,
        "max_duration_seconds": 180,
        "temperature": 1.0,
        "top_p": 1.0,
        "top_k": -1,
        "bootstrap_family_seed": 20261106,
        "bootstrap_draws": 10000,
        "gates_sha256": hashlib.sha256(await asyncio.to_thread(gates.read_bytes)).hexdigest(),
        "quote_sha256": hashlib.sha256(await asyncio.to_thread(quote.read_bytes)).hexdigest(),
        "advance_gate": {
            "positive_paired_delta": True,
            "min_interactive_only_pairs": 2,
            "min_verified_feedback_revision_families": 2,
            "budget_required_separately": True,
        },
    }
    if prompt_study:
        assert prompt_control is not None
        protocol["prompt_control_sha256"] = hashlib.sha256(control_bytes).hexdigest()
        protocol["prompt_example_sha256"] = hashlib.sha256(TOOL_EXAMPLE.encode()).hexdigest()
        protocol["prompt_instruction_sha256"] = {
            t.case_id: {
                v: hashlib.sha256(instruction(t, "interactive", v).encode()).hexdigest()
                for v in PROMPT_VARIANTS
            }
            for t in development_tasks()
        }
        protocol["advance_gate"] = {
            "positive_paired_delta": True,
            "min_tool_example_only_pairs": 2,
            "min_verified_feedback_revision_families": 2,
            "budget_required_separately": True,
        }
    if batch_study or limit_study:
        protocol["batch_control_sha256"] = hashlib.sha256(batch_bytes).hexdigest()
        protocol["prompt_variant"] = "original"
        protocol["feedback_criterion"] = (
            "batch_before_any_failed_public_test_then_later_patch_private_pass_v1"
        )
        protocol["advance_gate"] = {
            "positive_paired_delta": True,
            "min_batch_read_only_pairs": 2,
            "min_verified_feedback_revision_families": 2,
            "budget_required_separately": True,
        }
        if limit_study:
            protocol["path_limits"] = {"batch_read": 4, "batch_read_eight": 8}
            protocol["shared_read_character_limit"] = 5000
            protocol["advance_gate"] = {
                "positive_paired_delta": True,
                "min_batch_read_eight_only_pairs": 2,
                "min_verified_feedback_revision_families": 2,
                "budget_required_separately": True,
            }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2))
    (output / "policies.json").write_text(json.dumps({"base": uri}, indent=2))
    sources = _snapshot()
    (output / "source.json").write_text(json.dumps(sources, indent=2))
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for name in sorted(sources):
            archive.add(name, arcname=name, recursive=False)
    recorder = MetricRecorder(
        output / "tracking",
        RunSpec(
            output.name,
            "development",
            "base",
            hashlib.sha256((output / "source.json").read_bytes()).hexdigest(),
            group_size=1,
            learning_rate=0.0,
        ),
    )
    ledger = ROOT / "expansion-v2/calibrated-budget.json"
    audit = ROOT / "expansion-v2/reconciliation-01/audit.json"
    tasks = {t.case_id: t for t in development_tasks()}
    labels = layout.arms
    counts = dict.fromkeys(labels, 0)
    successes = dict.fromkeys(labels, 0)
    with CalibratedBudget(ledger, audit=audit) as budget:
        _no_pending(ledger)
        start = budget.committed
        (output / "admission.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "available_cny": str(budget.available),
                    "required_cny": str(len(rows) * RUN_BOUND),
                    "declared": len(rows),
                    "admitted": budget.available >= len(rows) * RUN_BOUND,
                    "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
                    "new_model_calls": 0,
                },
                indent=2,
            )
        )
        if budget.available < len(rows) * RUN_BOUND:
            raise ValueError("complete declared diagnostic cannot fit remaining global budget")
        (output / "budget-start.json").write_bytes(ledger.read_bytes())
        recorder.connect(mode=mode)
        service = trio.ServiceClient()
        sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B", model_path=uri)
        tokenizer = cast(Tokenizer, sampler.get_tokenizer())
        identity = PricedBackend(sampler, tokenizer, budget, "matched_initial").identity()
        _verify_tokenizer_identity(identity)
        (output / "tokenizer.json").write_text(json.dumps(identity, indent=2))
        for i, row in enumerate(rows):
            remaining = (len(rows) - i) * RUN_BOUND
            if budget.available < remaining or budget.committed - start + remaining > STAGE_CAP:
                raise ValueError("remaining complete queue cannot fit declared resources")
            row["status"] = "running"
            queue.write_text(json.dumps(rows, indent=2))
            backend = PricedBackend(
                sampler,
                tokenizer,
                budget,
                "matched_initial",
                evaluation_reserve=(len(rows) - i - 1) * RUN_BOUND,
            )
            result = await run_rollout(
                tasks[str(row["case_id"])],
                backend,
                policy_id="matched_initial",
                run_id=str(row["run_id"]),
                seed_value=int(str(row["seed"])),
                mode=cast(Literal["single", "interactive"], row["mode"]),
                output=output / str(row["run_id"]),
                host=DockerHost(),
                image=IMAGE,
                max_output_per_call=max_completion_per_call,
                prompt_variant=cast(PromptVariant, row.get("prompt_variant", "original")),
                read_variant=cast(ReadVariant, row.get("read_variant", "single_read")),
            )
            _no_pending(ledger)
            name = str(row[layout.comparison_key])
            row.update(
                {
                    "status": "completed",
                    "success": result["success"],
                    "tokens": result["total_tokens"],
                }
            )
            counts[name] += 1
            successes[name] += result["success"] is True
            queue.write_text(json.dumps(rows, indent=2))
            recorder.log(
                i,
                {
                    "eval/successes": float(sum(successes.values())),
                    "eval/declared": 32.0,
                    "rollout/completed": float(i + 1),
                    "usage/total_tokens": float(sum(int(str(r.get("tokens", 0))) for r in rows)),
                    "budget/stage_upper_cny": float(budget.committed - start),
                    "budget/cumulative_upper_cny": float(budget.committed),
                },
            )
        (output / "summary.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "declared": 32,
                    "completed": sum(counts.values()),
                    "counts": counts,
                    "successes": successes,
                    "stage_upper_cny": str(budget.committed - start),
                    "cumulative_upper_cny": str(budget.committed),
                    "pending_operations": 0,
                    "source_unchanged": _snapshot() == sources,
                    "training_updates": 0,
                    "actual_provider_bill_cny": None,
                },
                indent=2,
            )
        )
    recorder.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--quote", type=Path, required=True)
    parser.add_argument("--swanlab-mode", choices=("online", "offline"), default="offline")
    parser.add_argument("--max-completion-per-call", type=int, choices=(512, 1024), default=512)
    study = parser.add_mutually_exclusive_group()
    study.add_argument("--prompt-study", action="store_true")
    study.add_argument("--batch-study", action="store_true")
    study.add_argument("--limit-study", action="store_true")
    parser.add_argument("--prompt-control", type=Path)
    parser.add_argument("--batch-control", type=Path)
    args = parser.parse_args()
    trio.configure(request_timeout_seconds=600, sampling_timeout_seconds=180)
    asyncio.run(
        evaluate(
            args.output,
            args.gates,
            args.quote,
            mode=args.swanlab_mode,
            max_completion_per_call=args.max_completion_per_call,
            prompt_study=args.prompt_study,
            prompt_control=args.prompt_control,
            batch_study=args.batch_study,
            batch_control=args.batch_control,
            limit_study=args.limit_study,
        )
    )


if __name__ == "__main__":
    main()

"""Same paired evaluation with a proved .06 CNY final-test admission bound."""

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
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import ROOT
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.evaluate_run import verify_sources
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.rollout_v2 import run_rollout
from minidsh_rl.tasks_v2 import expansion_tasks
from minidsh_rl.train_v2 import _no_pending

RUN_BOUND = Decimal(".06")


def evaluation_queue(split: str) -> list[dict[str, object]]:
    """Two attempts per case/policy; same seed within each alternating-order pair."""
    if split not in {"validation", "test"}:
        raise ValueError("only frozen validation or test may be evaluated")
    rows: list[dict[str, object]] = []
    pair = 0
    seed_base = 25017 if split == "validation" else 35017
    for i, task in enumerate(t for t in expansion_tasks() if t.split == split):
        for repeat in range(2):
            order = ("base", "rl") if pair % 2 == 0 else ("rl", "base")
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


def policy_paths(training: Path) -> dict[str, str]:
    """A zero-initialized adapter from this training run is the matched Base."""
    summary = json.loads((training / "summary.json").read_text())
    final = json.loads((training / "final_weights.json").read_text())
    paths = {"base": summary["initial_model_path"], "rl": final["path"]}
    if not all(isinstance(p, str) and p.startswith("trio://") for p in paths.values()):
        raise ValueError("missing persistent policy checkpoint")
    return paths


def require_final_test(training: Path, decision: Path | None) -> None:
    """Opening test needs an explicit frozen decision tied to the final checkpoint."""
    if decision is None:
        raise ValueError("final test needs a recorded decision")
    verdict = json.loads(decision.read_text())
    if (
        verdict.get("allow_final_test") is not True
        or verdict.get("parent_summary_sha256")
        != hashlib.sha256((training / "summary.json").read_bytes()).hexdigest()
    ):
        raise ValueError("final test not admitted for this checkpoint")
    amendment = ROOT / "expansion-v2/budget-priority-amendment-02.json"
    frozen = json.loads(amendment.read_text())
    if hashlib.sha256(amendment.read_bytes()).hexdigest() != verdict["amendment_sha256"]:
        raise ValueError("final test amendment changed")
    proof_path = Path(frozen["bound_proof"])
    if hashlib.sha256(proof_path.read_bytes()).hexdigest() != frozen["bound_proof_sha256"]:
        raise ValueError("test bound proof changed")
    proof = json.loads(proof_path.read_text())
    if (
        Decimal(proof["combined_upper_cny"]) >= RUN_BOUND
        or Decimal(proof["admitted_run_upper_cny"]) != RUN_BOUND
    ):
        raise ValueError("run allocation does not cover proved Token bound")
    for file, digest in proof["sources"].items():
        if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest:
            raise ValueError("Token budget/price source changed")


def _prepare(
    output: Path,
    training: Path,
    split: str,
    stage_cap: Decimal,
    decision: Path | None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    verify_sources(training)
    if split == "test":
        require_final_test(training, decision)
    rows = evaluation_queue(split)
    required = len(rows) * RUN_BOUND
    if not stage_cap.is_finite() or stage_cap < required:
        raise ValueError("stage allocation cannot fit the full declared evaluation")
    paths = policy_paths(training)
    output.mkdir(parents=True, exist_ok=False)
    for i, row in enumerate(rows):
        row["run_id"] = f"{output.name}_{i:03}_{row['policy']}"
    queue = output / "queue.json"
    queue.write_text(json.dumps(rows, indent=2))
    (output / "policies.json").write_text(json.dumps(paths, indent=2))
    (output / "protocol.json").write_text(
        json.dumps(
            {
                "split": split,
                "stage_cap_cny": str(stage_cap),
                "declared": len(rows),
                "run_bound_cny": str(RUN_BOUND),
                "training_summary_hash": hashlib.sha256(
                    (training / "summary.json").read_bytes()
                ).hexdigest(),
                "evaluator_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    return rows, paths


async def evaluate(  # noqa: PLR0913 - sequential paired protocol
    output: Path,
    training: Path,
    image: str,
    *,
    split: str,
    stage_cap: Decimal,
    decision: Path | None = None,
) -> None:
    rows, paths = await asyncio.to_thread(_prepare, output, training, split, stage_cap, decision)
    required = len(rows) * RUN_BOUND
    queue = output / "queue.json"
    task_map = {t.case_id: t for t in expansion_tasks()}
    audit = ROOT / "expansion-v2/reconciliation-01/audit.json"
    with CalibratedBudget(ROOT / "expansion-v2/calibrated-budget.json", audit=audit) as budget:
        _no_pending(budget.path)
        if budget.available < required:
            raise ValueError("full evaluation does not fit cumulative budget")
        baseline = budget.committed
        service = trio.ServiceClient()
        samplers = {
            name: service.create_sampling_client(base_model="Qwen/Qwen3.5-4B", model_path=uri)
            for name, uri in paths.items()
        }
        tokenizers = {
            name: cast(Tokenizer, sampler.get_tokenizer()) for name, sampler in samplers.items()
        }
        expected = json.loads((training / "tokenizer.json").read_text())
        host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
        for i, row in enumerate(rows):
            remaining = (len(rows) - i) * RUN_BOUND
            if budget.available < remaining or budget.committed - baseline + remaining > stage_cap:
                raise ValueError("remaining evaluation no longer fits allocation; retain queue")
            name = str(row["policy"])
            backend = PricedBackend(
                samplers[name],
                tokenizers[name],
                budget,
                name,
                evaluation_reserve=remaining - RUN_BOUND,
            )
            identity = backend.identity()
            if any(identity[k] != expected[k] for k in ("template_hash", "vocabulary_hash")):
                raise ValueError("evaluation tokenizer differs from training")
            row["status"] = "running"
            queue.write_text(json.dumps(rows, indent=2))
            result = await run_rollout(
                task_map[str(row["case_id"])],
                backend,
                policy_id=name,
                run_id=str(row["run_id"]),
                seed=int(str(row["seed"])),
                output=output / str(row["run_id"]),
                host=host,
                image=image,
                instruction_suffix="",
            )
            _no_pending(budget.path)
            row.update(
                status=result.status,
                success=result.success,
                tokens=result.total_tokens,
                tool_calls=result.tool_calls,
                duration_ms=result.duration_ms,
                failure_type=result.failure_type,
            )
            queue.write_text(json.dumps(rows, indent=2))
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
        (output / "summary.json").write_text(
            json.dumps(
                {
                    "declared": len(rows),
                    "completed": len(rows),
                    "split": split,
                    "stage_spend_upper_cny": str(budget.committed - baseline),
                    "global_committed_upper_cny": str(budget.committed),
                },
                indent=2,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--split", choices=("test",), required=True)
    parser.add_argument("--stage-cap-cny", type=Decimal, required=True)
    parser.add_argument("--decision", type=Path)
    args = parser.parse_args()
    asyncio.run(
        evaluate(
            args.output,
            args.training,
            args.image,
            split=args.split,
            stage_cap=args.stage_cap_cny,
            decision=args.decision,
        )
    )


if __name__ == "__main__":
    main()

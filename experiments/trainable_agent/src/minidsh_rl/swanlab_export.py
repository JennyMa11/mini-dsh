"""Rebuild explicitly labelled historical metric views; no training/model calls."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from minidsh_rl.tracking import MetricRecorder, RunSpec


def export_history(
    output: Path, *, mode: Literal["offline", "online"], workspace: str | None = None
) -> list[dict[str, str | None]]:
    """Hash-check frozen report inputs, then export only aggregate numeric fields."""
    source = Path("docs/results/trainable-agent-expansion-2026-11-07.json")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report = json.loads(source.read_text())
    for name, expected in report["input_hashes"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("historical report evidence changed")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    training = MetricRecorder(
        output / "training",
        RunSpec("imported-v2-grpo-train", "historical_train", "grpo_centered", digest),
    )
    training.connect(mode=mode, workspace=workspace)
    directory = Path("benchmark-results/trainable_agent/expansion-v2/pilot-train-01")
    queue = json.loads((directory / "queue.json").read_text())
    tokens = updates = 0
    for row in queue:
        slot = row["slot"]
        group = json.loads((directory / f"group_{slot:03}.json").read_text())
        tokens += row["tokens"]
        updates += row["updated"]
        training.log(
            slot,
            {
                "reward/mean": sum(group["rewards"]) / len(group["rewards"]),
                "rollout/completed": float((slot + 1) * 4),
                "rollout/declared": 160.0,
                "rollout/zero_signal": float(not row["updated"]),
                "usage/total_tokens": float(tokens),
                "train/updates": float(updates),
            },
        )
    results.append(training.finish())
    for policy in ("base", "rl"):
        algorithm: Literal["base", "grpo_centered"] = (
            "base" if policy == "base" else "grpo_centered"
        )
        recorder = MetricRecorder(
            output / policy,
            RunSpec(f"imported-v2-test-{policy}", "historical_test", algorithm, digest),
        )
        recorder.connect(mode=mode, workspace=workspace)
        row = report["test"]["policies"][policy]
        numeric = {
            "eval/successes": row["successes"],
            "eval/declared": row["declared"],
            "eval/verified": row["verified"],
            "eval/coverage": row["coverage"],
            "eval/success_rate": row["success_rate_declared"],
            "eval/total_tokens": row["total_tokens"],
            "eval/tokens_per_success": row["tokens_per_success"],
            "eval/mean_tool_calls": row["mean_tool_calls"],
            "eval/latency_p50_ms": row["latency_p50_ms"],
            "eval/latency_p95_ms": row["latency_p95_ms"],
            "eval/format_rejection_rate": report["tool_observations"]["policies"][policy][
                "format_rejection_rate"
            ],
            "budget/cumulative_upper_cny": float(
                report["budget"]["cumulative_committed_upper_cny"]
            ),
        }
        recorder.log(0, {key: float(value) for key, value in numeric.items() if value is not None})
        results.append(recorder.finish())
    (output / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "historical_import": True,
                "new_model_calls": 0,
                "source_sha256": digest,
                "runs": results,
                "limitations": (
                    "single training seed; opened v2 test; exploratory, not a new GSPO evaluation"
                ),
            },
            indent=2,
        )
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--workspace")
    args = parser.parse_args()
    export_history(args.output, mode=args.mode, workspace=args.workspace)


if __name__ == "__main__":
    main()

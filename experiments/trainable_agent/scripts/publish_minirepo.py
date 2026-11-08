"""Upload only allowlisted aggregate development metrics, never task/trace content."""

import argparse
import hashlib
import json
from pathlib import Path

from minidsh_rl.minirepo_studies import BATCH_LIMIT, BATCH_READ, PROMPT, WORKFLOW, study_layout
from minidsh_rl.tracking import MetricRecorder, RunSpec

DECLARED = 32


def publish(analysis: Path, diagnostics: Path, output: Path, cap: int) -> None:
    report = json.loads(analysis.read_text())
    extra = json.loads(diagnostics.read_text())
    if report["declared"] != DECLARED or report["completed"] != DECLARED:
        raise ValueError("complete declared development cohort required")
    layout = study_layout(str(report.get("phase", WORKFLOW.phase)))
    arms = layout.arms
    if set(report["policies"]) != set(arms) or set(extra["policies"]) != set(arms):
        raise ValueError("analysis/diagnostic arms disagree")
    study = (
        "batch-limit"
        if layout == BATCH_LIMIT
        else "batch-read"
        if layout == BATCH_READ
        else "tool-example"
        if layout == PROMPT
        else "workflow"
    )
    output.mkdir(parents=True, exist_ok=False)
    urls = []
    for mode in arms:
        spec = RunSpec(
            f"minirepo-cap{cap}-{study}-{mode}",
            "development",
            "base",
            hashlib.sha256(analysis.read_bytes()).hexdigest(),
            group_size=1,
            learning_rate=0.0,
        )
        recorder = MetricRecorder(output / mode, spec)
        recorder.connect(mode="online")
        row = report["policies"][mode]
        values = {
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
            "eval/format_rejection_rate": extra["policies"][mode]["format_rejection_rate"],
            "budget/stage_upper_cny": float(extra["policies"][mode]["sample_upper_cny"]),
        }
        recorder.log(0, {key: float(value) for key, value in values.items() if value is not None})
        urls.append(recorder.finish())
    recorder = MetricRecorder(
        output / "paired",
        RunSpec(
            f"minirepo-cap{cap}-{study}-delta",
            "development",
            "base",
            hashlib.sha256(analysis.read_bytes()).hexdigest(),
            group_size=1,
            learning_rate=0.0,
        ),
    )
    recorder.connect(mode="online")
    paired = report["paired"]
    recorder.log(
        0,
        {
            "eval/delta_success_rate": paired["difference"],
            "eval/delta_ci95_low": paired["ci95"][0],
            "eval/delta_ci95_high": paired["ci95"][1],
        },
    )
    urls.append(recorder.finish())
    (output / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "new_model_calls": 0,
                "phase": "development",
                "cap": cap,
                "runs": urls,
                "analysis_sha256": hashlib.sha256(analysis.read_bytes()).hexdigest(),
                "diagnostics_sha256": hashlib.sha256(diagnostics.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cap", type=int, choices=(512, 1024), required=True)
    args = parser.parse_args()
    publish(args.analysis, args.diagnostics, args.output, args.cap)


if __name__ == "__main__":
    main()

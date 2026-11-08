"""Handle saved-report rendering and Trace inspection commands without starting an Agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from minidsh.evaluation import MetricPoint, TraceRun, build_replay, diff_traces, pareto
from minidsh.evaluation.diff import to_markdown as diff_markdown
from minidsh.evaluation.harness_metrics import ScoringEvidence, score_harness
from minidsh.evaluation.replay import to_markdown as replay_markdown


def report_command(args: argparse.Namespace) -> int:
    """Render a saved benchmark or ablation report to stdout or the requested file."""
    path = Path(args.path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read report: {path}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"report must be a JSON object: {path}"
        raise ValueError(msg)
    rendered = (
        _render_ablation_report(payload)
        if "variants" in payload
        else _render_benchmark_report(payload)
    )
    if args.markdown is not None:
        destination = Path(args.markdown)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"wrote {destination}\n")
    else:
        sys.stdout.write(rendered)
    return 0


def _render_ablation_report(payload: dict[str, Any]) -> str:
    variants = payload.get("variants")
    if not isinstance(variants, list):
        msg = "ablation report has no variants"
        raise ValueError(msg)
    lines = [f"# Ablation report: {payload.get('manifest', 'unknown')}", ""]
    model = payload.get("model")
    if isinstance(model, dict):
        lines += [f"- Model: `{model.get('label')}`", ""]
    lines += [
        "| Variant | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    points: list[MetricPoint] = []
    for item in variants:
        if not isinstance(item, dict):
            continue
        configuration = item.get("configuration", {})
        aggregate = item.get("aggregate", {})
        name = str(configuration.get("name", "unknown"))
        points.append(MetricPoint.from_aggregate(name, aggregate))
        lines.append(
            f"| {name} | {float(aggregate.get('success_rate', 0.0)):.0%} | "
            f"{float(aggregate.get('pass_at_k', 0.0)):.0%} | "
            f"{float(aggregate.get('average_steps', 0.0)):.2f} | "
            f"{float(aggregate.get('average_tokens', 0.0)):.1f} | "
            f"{float(aggregate.get('total_cost_usd', 0.0)):.6f} | "
            f"{float(aggregate.get('cost_per_success_usd', 0.0)):.6f} |"
        )
    lines += ["", pareto.to_markdown(points, pareto.analyze(points)).rstrip(), ""]
    return "\n".join(lines) + "\n"


def _render_benchmark_report(payload: dict[str, Any]) -> str:
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict):
        msg = "benchmark report has no aggregate"
        raise ValueError(msg)
    lines = [f"# Benchmark report: {payload.get('name', 'unknown')}", ""]
    model = payload.get("model")
    if isinstance(model, dict):
        lines += [f"- Model: `{model.get('label')}`", ""]
    for key in sorted(aggregate):
        lines.append(f"- `{key}`: {aggregate[key]}")
    layers = payload.get("failure_layers")
    if isinstance(layers, dict):
        lines += ["", "## Outcome by layer", ""]
        lines += [f"- `{name}`: {value}" for name, value in sorted(layers.items())]
    categories = payload.get("failure_categories")
    if isinstance(categories, dict):
        lines += ["", "## Failure categories", ""]
        lines += [f"- `{name}`: {value}" for name, value in sorted(categories.items())]
    return "\n".join(lines) + "\n"


def trace_replay(args: argparse.Namespace) -> int:
    """Render a saved run as JSON or a readable sequence of observations."""
    trace = TraceRun.from_file(Path(args.path), run_id=args.run_id)
    if args.as_json:
        sys.stdout.write(f"{json.dumps(build_replay(trace).to_json(), indent=2, sort_keys=True)}\n")
    else:
        sys.stdout.write(
            replay_markdown(build_replay(trace), preview_chars=int(args.preview_chars))
        )
    return 0


def trace_diff(args: argparse.Namespace) -> int:
    """Compare two saved traces without executing their recorded actions."""
    left = TraceRun.from_file(Path(args.left))
    right = TraceRun.from_file(Path(args.right))
    result = diff_traces(
        left,
        right,
        left_label=args.left_label or Path(args.left).stem,
        right_label=args.right_label or Path(args.right).stem,
    )
    if args.as_json:
        sys.stdout.write(f"{json.dumps(result.to_json(), indent=2, sort_keys=True)}\n")
    else:
        sys.stdout.write(diff_markdown(result))
    return 0


def trace_metrics(args: argparse.Namespace) -> int:
    """Score a saved trace with an optional independent verification sidecar."""
    trace = TraceRun.from_file(Path(args.path), run_id=args.run_id)
    evidence = ScoringEvidence.from_file(args.evidence) if args.evidence else None
    report = score_harness(trace, evidence=evidence)
    sys.stdout.write(f"{json.dumps(report.to_json(), indent=2, sort_keys=True)}\n")
    return 0

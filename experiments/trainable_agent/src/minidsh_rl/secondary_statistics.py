"""Paired family-cluster intervals for efficiency, retaining failed-run consumption."""

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from minidsh_rl.report import _percentile

BOOTSTRAP_REPETITIONS = 10000


def measures(rows: list[dict[str, object]]) -> dict[str, float | None]:
    """Ratios use aggregate resources / all successes; no success means undefined."""
    base = [r for r in rows if r["policy"] == "base"]
    rl = [r for r in rows if r["policy"] == "rl"]
    if not base or len(base) != len(rl):
        raise ValueError("complete paired policies required")
    b_s, r_s = sum(r["success"] is True for r in base), sum(r["success"] is True for r in rl)
    b_t = sum(int(str(r["tokens"])) for r in base)
    r_t = sum(int(str(r["tokens"])) for r in rl)
    b_calls = sum(int(str(r["tool_calls"])) for r in base)
    r_calls = sum(int(str(r["tool_calls"])) for r in rl)
    b_ms = sum(float(str(r["duration_ms"])) for r in base)
    r_ms = sum(float(str(r["duration_ms"])) for r in rl)
    return {
        "token_per_success_relative_change": r_t * b_s / (b_t * r_s) - 1
        if b_t and b_s and r_s
        else None,
        "total_token_relative_change": r_t / b_t - 1 if b_t else None,
        "mean_tool_call_difference": (r_calls - b_calls) / len(base),
        "mean_e2e_latency_ms_difference": (r_ms - b_ms) / len(base),
    }


def analyze(report: dict[str, object]) -> dict[str, object]:
    """Whole families are resampled; undefined bootstrap ratios remain unknown."""
    rows = report["runs"]
    if not isinstance(rows, list) or not rows or len(rows) % 2:
        raise ValueError("complete paired rows required")
    clusters: dict[str, list[dict[str, object]]] = defaultdict(list)
    for first, second in zip(rows[::2], rows[1::2], strict=True):
        if (
            not isinstance(first, dict)
            or not isinstance(second, dict)
            or {first.get("policy"), second.get("policy")} != {"base", "rl"}
            or any(first.get(k) != second.get(k) for k in ("family", "case_id", "repeat", "seed"))
            or type(first.get("success")) is not bool
            or type(second.get("success")) is not bool
        ):
            raise ValueError("mismatched pair or unverified result")
        clusters[str(first["family"])].extend([first, second])
    names = sorted(clusters)
    point = measures(rows)
    rng = random.Random(20261106)
    draws: dict[str, list[float]] = {k: [] for k in point}
    for _ in range(BOOTSTRAP_REPETITIONS):
        selected = rng.choices(names, k=len(names))
        result = measures([r for name in selected for r in clusters[name]])
        for key, value in result.items():
            if value is not None:
                draws[key].append(value)
    return {
        "families": len(names),
        "pairs": len(rows) // 2,
        "bootstrap_repetitions": BOOTSTRAP_REPETITIONS,
        "seed": 20261106,
        "quantile_method": "nearest_rank",
        "metrics": {
            key: {
                "value": value,
                "ci95": [_percentile(draws[key], 0.025), _percentile(draws[key], 0.975)]
                if len(names) > 1 and draws[key]
                else None,
                "defined_draws": len(draws[key]),
                "undefined_draws": BOOTSTRAP_REPETITIONS - len(draws[key]),
            }
            for key, value in point.items()
        },
        "limitations": [
            "single training seed; exploratory intervals",
            "undefined zero-success ratios are excluded from their interval; counts reported",
            "latency includes uncontrolled host/network/service variation",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(json.loads(args.input.read_text()))
    result["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    result["scorer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.output.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()

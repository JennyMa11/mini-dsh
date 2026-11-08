"""Compare two complete development cohorts, verifying their single-variable protocol."""

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from minidsh_rl.report import paired_interval

DECLARED = 32


def compare(first: Path, second: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - independent cohort checks
    left = json.loads((first / "protocol.json").read_text())
    right = json.loads((second / "protocol.json").read_text())
    excluded = {"max_completion_per_call", "gates_sha256", "quote_sha256"}
    if {k: v for k, v in left.items() if k not in excluded} != {
        k: v for k, v in right.items() if k not in excluded
    }:
        raise ValueError("protocol changed beyond declared output cap")
    if (left["max_completion_per_call"], right["max_completion_per_call"]) != (512, 1024):
        raise ValueError("unexpected output cap pair")
    if json.loads((first / "policies.json").read_text()) != json.loads(
        (second / "policies.json").read_text()
    ):
        raise ValueError("policy checkpoint changed")
    if json.loads((first / "tokenizer.json").read_text()) != json.loads(
        (second / "tokenizer.json").read_text()
    ):
        raise ValueError("tokenizer/renderer changed")
    source_maps = [json.loads((p / "source.json").read_text()) for p in (first, second)]
    if source_maps[0].keys() != source_maps[1].keys():
        raise ValueError("source inventory changed")
    changed = {p for p, digest in source_maps[0].items() if source_maps[1][p] != digest}
    expected = {
        "experiments/trainable_agent/src/minidsh_rl/minirepo_rollout.py",
        "experiments/trainable_agent/src/minidsh_rl/minirepo_experiment.py",
    }
    if changed != expected:
        raise ValueError("unexpected source changes beyond output-cap configuration")
    if any(
        json.loads((p / "summary.json").read_text())["source_unchanged"] is not True
        for p in (first, second)
    ):
        raise ValueError("runtime source drift")
    cohorts = []
    bindings = {}
    for directory in (first, second):
        rows = {}
        for item in json.loads((directory / "queue.json").read_text()):
            if item["status"] != "completed":
                raise ValueError("complete cohort required")
            path = directory / item["run_id"] / "result.json"
            result = json.loads(path.read_text())
            key = (result["case_id"], result["seed"], result["mode"])
            rows[key] = result
            bindings[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        if len(rows) != DECLARED:
            raise ValueError("full declared cohort required")
        cohorts.append(rows)
    if cohorts[0].keys() != cohorts[1].keys():
        raise ValueError("case/seed/mode pairing changed")
    changes = {}
    for mode in ("single", "interactive"):
        pairs = [
            (str(a["family"]), bool(a["success"]), bool(cohorts[1][key]["success"]))
            for key, a in cohorts[0].items()
            if key[2] == mode
        ]
        changes[mode] = paired_interval(pairs)
        changes[mode]["limitation"] = (
            "four development families; exploratory output-cap comparison, no RL"
        )
    interaction: dict[str, list[float]] = defaultdict(list)
    for key, a in cohorts[0].items():
        if key[2] != "single":
            continue
        other = (key[0], key[1], "interactive")
        values = [float(c[other]["success"]) - float(c[key]["success"]) for c in cohorts]
        interaction[str(a["family"])].append(values[1] - values[0])
    names = sorted(interaction)
    rng = random.Random(20261106)
    draws = []
    for _ in range(10000):
        picked = rng.choices(names, k=len(names))
        draws.append(
            sum(sum(interaction[n]) for n in picked) / sum(len(interaction[n]) for n in picked)
        )
    draws.sort()
    return {
        "schema_version": 1,
        "single_variable_protocol_passed": True,
        "declared_per_cohort": 32,
        "output_cap_effect_by_mode": changes,
        "workflow_delta_difference": {
            "value": sum(sum(v) for v in interaction.values())
            / sum(len(v) for v in interaction.values()),
            "ci95": [
                draws[math.ceil(0.025 * len(draws)) - 1],
                draws[math.ceil(0.975 * len(draws)) - 1],
            ],
            "families": len(names),
            "bootstrap_draws": 10000,
            "seed": 20261106,
            "exploratory": True,
        },
        "input_sha256": bindings,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "development-selected output cap; not a held-out confirmation",
            "one policy; no RL updates",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        json.dump(compare(args.first, args.second), stream, indent=2)


if __name__ == "__main__":
    main()

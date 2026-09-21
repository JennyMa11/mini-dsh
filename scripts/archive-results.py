#!/usr/bin/env python3
"""Archive curated experiment results from ``benchmark-results/`` into ``docs/results/``.

Raw JSONL traces and full JSON reports under ``benchmark-results/`` are regenerable and
git-ignored. This script copies the human-readable Markdown reports that belong in the
repository and derives ``docs/results/summary.json`` from the machine-readable ones, so the
curated numbers in the docs always come from an actual run rather than being hand-edited.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "benchmark-results"
TARGET = ROOT / "docs" / "results"

BASELINES = ("smoke", "core", "stress")
ABLATION_SUITES = ("context", "tool_output", "permission")
ABLATION_MANIFESTS = ("core", "stress")
# Real-model reports are archived alongside the scripted ones, but they cannot be
# regenerated without a paid API key, so they are never *required*: re-archiving
# the scripted suites must not delete the only committed copy of a paid run.
REAL_MODEL_REPORTS = ("repo-real",)

# The tariff a real-model run was billed under.  It cannot be recovered from the
# report or guessed from the totals — DeepSeek bills off-peak at half of peak, so
# the same run has two correct costs — and a cost number is only meaningful next
# to the tariff that produced it.  The operator therefore has to state it.
_RATE_CARD_VARS = (
    "MINIDSH_INPUT_USD_PER_MILLION",
    "MINIDSH_OUTPUT_USD_PER_MILLION",
    "MINIDSH_CACHED_INPUT_USD_PER_MILLION",
)


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        parsed: dict[str, Any] = json.load(handle)
    return parsed


def _archive_markdown(name: str) -> bool:
    source = SOURCE / f"{name}.md"
    if not source.exists():
        sys.stderr.write(f"missing report, skipping: {source}\n")
        return False
    shutil.copyfile(source, TARGET / source.name)
    return True


def _baseline_summary(name: str) -> dict[str, Any] | None:
    path = SOURCE / f"{name}.json"
    if not path.exists():
        return None
    report = _load(path)
    return {
        "schema_version": report["schema_version"],
        "aggregate": report["aggregate"],
        "categories": report["categories"],
        "failure_types": report["failure_types"],
    }


def _ablation_summary(name: str) -> dict[str, Any] | None:
    path = SOURCE / f"{name}.json"
    if not path.exists():
        return None
    report = _load(path)
    return {
        "manifest": report["manifest"],
        "variants": [
            {
                "name": variant["configuration"]["name"],
                "configuration": variant["configuration"],
                "aggregate": variant["aggregate"],
            }
            for variant in report["variants"]
        ],
    }


def _declared_rate_card() -> dict[str, float] | None:
    """Read the tariff from the environment, or explain what is missing."""
    rates: dict[str, float] = {}
    for variable in _RATE_CARD_VARS:
        raw = os.environ.get(variable)
        if raw is None:
            sys.stderr.write(
                f"{variable} is not set, so the tariff behind the real-model cost "
                "figures is unknown; leaving the previous real_model block in place\n"
            )
            return None
        rates[variable] = float(raw)
    return {
        "input_usd_per_million": rates["MINIDSH_INPUT_USD_PER_MILLION"],
        "output_usd_per_million": rates["MINIDSH_OUTPUT_USD_PER_MILLION"],
        "cached_input_usd_per_million": rates["MINIDSH_CACHED_INPUT_USD_PER_MILLION"],
    }


def _real_model_summary(name: str, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    path = SOURCE / f"{name}.json"
    if not path.exists():
        sys.stderr.write(f"missing report, skipping: {path}\n")
        return _carried_forward(previous)
    rates = _declared_rate_card()
    if rates is None:
        return _carried_forward(previous)
    report = _load(path)
    conditions = report.get("model")
    profile = conditions.get("profile") if isinstance(conditions, dict) else None
    return {
        "name": report["name"],
        "schema_version": report["schema_version"],
        "aggregate": report["aggregate"],
        "failure_layers": report["failure_layers"],
        "repeat_variance": report["repeat_variance"],
        "rate_card_usd_per_million": rates,
        # Recorded inside the report since v0.3.0; absent for older runs, which
        # is itself worth knowing when reading the numbers.
        "model": conditions.get("label") if isinstance(conditions, dict) else None,
        "thinking": profile.get("thinking") if isinstance(profile, dict) else None,
    }


def _carried_forward(previous: dict[str, Any] | None) -> dict[str, Any] | None:
    carried = (previous or {}).get("real_model")
    return carried if isinstance(carried, dict) else None


def main() -> int:
    if not SOURCE.is_dir():
        sys.stderr.write(f"no results directory: {SOURCE}\n")
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)

    previous = _load(TARGET / "summary.json") if (TARGET / "summary.json").exists() else None

    archived = 0
    for name in BASELINES:
        archived += _archive_markdown(name)
    for manifest in ABLATION_MANIFESTS:
        for suite in ABLATION_SUITES:
            archived += _archive_markdown(f"{manifest}-{suite}-ablation")
    for name in REAL_MODEL_REPORTS:
        archived += _archive_markdown(name)

    # The scripted rate card is a property of the archived scripted runs, so it is
    # carried forward rather than re-read from the environment: the real-model
    # archive uses the same MINIDSH_*_USD_PER_MILLION variables, and reading them
    # here would silently re-price every committed baseline number.  To re-price
    # the baselines deliberately, remove summary.json and archive with the rates set.
    pricing = previous.get("pricing_usd_per_million") if previous else None
    if not isinstance(pricing, dict):
        pricing = {
            "input": float(os.environ.get("MINIDSH_INPUT_USD_PER_MILLION", "3.0")),
            "output": float(os.environ.get("MINIDSH_OUTPUT_USD_PER_MILLION", "15.0")),
        }

    summary: dict[str, Any] = {
        "model": "scripted",
        "seed": 0,
        "pricing_usd_per_million": pricing,
        "baselines": {
            name: value for name in BASELINES if (value := _baseline_summary(name)) is not None
        },
        "ablations": {
            f"{manifest}-{suite}": value
            for manifest in ABLATION_MANIFESTS
            for suite in ABLATION_SUITES
            if (value := _ablation_summary(f"{manifest}-{suite}-ablation")) is not None
        },
    }
    real_model = {
        name: value
        for name in REAL_MODEL_REPORTS
        if (value := _real_model_summary(name, previous)) is not None
    }
    if real_model:
        summary["real_model"] = real_model

    with (TARGET / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    sys.stdout.write(f"archived {archived} Markdown reports and summary.json into {TARGET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

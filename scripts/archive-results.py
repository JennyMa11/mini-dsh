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


def main() -> int:
    if not SOURCE.is_dir():
        sys.stderr.write(f"no results directory: {SOURCE}\n")
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)

    archived = 0
    for name in BASELINES:
        archived += _archive_markdown(name)
    for manifest in ABLATION_MANIFESTS:
        for suite in ABLATION_SUITES:
            archived += _archive_markdown(f"{manifest}-{suite}-ablation")

    summary: dict[str, Any] = {
        "model": "scripted",
        "seed": 0,
        "pricing_usd_per_million": {
            "input": float(os.environ.get("MINIDSH_INPUT_USD_PER_MILLION", "3.0")),
            "output": float(os.environ.get("MINIDSH_OUTPUT_USD_PER_MILLION", "15.0")),
        },
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

    with (TARGET / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    sys.stdout.write(f"archived {archived} Markdown reports and summary.json into {TARGET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

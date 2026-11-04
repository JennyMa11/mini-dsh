#!/usr/bin/env bash
# Run the whole reproducible experiment matrix in one command.
#
#   scripts/run-all-experiments.sh
#
# Produces, under benchmark-results/:
#   smoke.json, core.json, stress.json            baselines
#   core-<suite>-ablation.json  (suite: context | tool_output | permission)
#   stress-<suite>-ablation.json
#
# Raw per-task traces are written next to each report under
# benchmark-results/*-artifacts/traces/. They are regenerable and therefore not
# committed; docs/results/ holds the curated summary instead.
set -euo pipefail
cd "$(dirname "$0")/.."

export MINIDSH_INPUT_USD_PER_MILLION="${MINIDSH_INPUT_USD_PER_MILLION:-3.0}"
export MINIDSH_OUTPUT_USD_PER_MILLION="${MINIDSH_OUTPUT_USD_PER_MILLION:-15.0}"

mkdir -p benchmark-results

echo "== baselines =="
scripts/run-benchmark.sh benchmarks/smoke/manifest.json benchmark-results/smoke.json
scripts/run-benchmark.sh benchmarks/core/manifest.json benchmark-results/core.json
scripts/run-benchmark.sh benchmarks/stress/manifest.json benchmark-results/stress.json

for suite in context tool_output permission; do
  for manifest in core stress; do
    echo "== ${manifest} / ${suite} =="
    scripts/run-ablation.sh "$suite" \
      "benchmarks/${manifest}/manifest.json" \
      "benchmark-results/${manifest}-${suite}-ablation.json"
  done
done

echo "== archiving =="
uv run python scripts/archive_results.py

echo "done: $(find docs/results -maxdepth 1 -name '*.md' | wc -l | tr -d ' ') Markdown reports in docs/results"

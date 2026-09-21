#!/usr/bin/env bash
# One command to run a single-variable ablation suite.
#
#   scripts/run-ablation.sh [suite] [manifest] [output]
#
# suite: standard | context | tool_output | permission
#
# Each suite runs the same manifest once per variant, so every difference in the
# comparison table is attributable to the one harness variable that changed.
set -euo pipefail
cd "$(dirname "$0")/.."

SUITE="${1:-context}"
MANIFEST="${2:-benchmarks/core/manifest.json}"
OUTPUT="${3:-benchmark-results/${SUITE}-ablation.json}"

uv run mini-dsh ablation "$MANIFEST" \
  --suite "$SUITE" \
  --output "$OUTPUT" \
  --allow-write \
  --allow-shell \
  --input-usd-per-million "${MINIDSH_INPUT_USD_PER_MILLION:-3.0}" \
  --output-usd-per-million "${MINIDSH_OUTPUT_USD_PER_MILLION:-15.0}" \
  --verify-command python

echo "wrote ${OUTPUT}, ${OUTPUT%.json}.jsonl, ${OUTPUT%.json}.md"

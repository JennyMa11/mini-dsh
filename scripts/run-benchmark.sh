#!/usr/bin/env bash
# One command to run the full reproducible benchmark suite.
#
#   scripts/run-benchmark.sh [manifest] [output]
#
# Defaults to the 25-task core suite. Every task runs in an isolated copy of its
# fixture, and success is decided by an independent verifier command, never by the
# model's own claim.
set -euo pipefail
cd "$(dirname "$0")/.."

MANIFEST="${1:-benchmarks/core/manifest.json}"
OUTPUT="${2:-benchmark-results/core.json}"

uv run mini-dsh benchmark "$MANIFEST" \
  --output "$OUTPUT" \
  --markdown "${OUTPUT%.json}.md" \
  --allow-write \
  --allow-shell \
  --input-usd-per-million "${MINIDSH_INPUT_USD_PER_MILLION:-3.0}" \
  --output-usd-per-million "${MINIDSH_OUTPUT_USD_PER_MILLION:-15.0}" \
  --verify-command python

echo "wrote ${OUTPUT}, ${OUTPUT%.json}.jsonl, ${OUTPUT%.json}.md"
echo "raw traces: $(dirname "$OUTPUT")/$(basename "${OUTPUT%.json}")-artifacts/traces"

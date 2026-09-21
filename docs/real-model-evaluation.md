# Real-model evaluation

The scripted suites answer one question: **does the harness do what it promises?**
They cannot answer the other question: **is the agent any good?** A deterministic
response script makes the step sequence predictable, which is exactly what you want
when you are testing Patch application, permission enforcement, or budget accounting —
and exactly what you must not quote as a capability result.

This document is the protocol for the second question.

## 1. Provider profiles

A profile pins the endpoint, the model name, and the credential variable. Pricing is
deliberately **not** pinned: published rates change without notice, and a stale rate
card silently corrupts every cost number in a report.

| Profile | Endpoint | Credential variable | Rate card |
| --- | --- | --- | --- |
| `deepseek` | `api.deepseek.com/v1/chat/completions` | `DEEPSEEK_API_KEY` | must be declared |
| `deepseek-reasoner` | same | `DEEPSEEK_API_KEY` | must be declared |
| `openai` | `api.openai.com/v1/chat/completions` | `OPENAI_API_KEY` | must be declared |
| `ollama` | `127.0.0.1:11434/v1/chat/completions` | none required | declared as free |

```bash
export DEEPSEEK_API_KEY=...
export MINIDSH_MODEL=deepseek-chat          # optional; overrides the profile default
```

`--endpoint`, `--model` and `--api-key-env` override the profile field by field, and
`MINIDSH_MODEL_PROFILE` selects a profile without a flag.

### Why a real provider refuses to run without a rate card

```text
$ mini-dsh run "fix the tests" --model-profile deepseek
mini-dsh: profile 'deepseek' has no declared rate card, so cost cannot be reported.
Pass --input-usd-per-million and --output-usd-per-million ... or pass --allow-unpriced.
```

A zero rate card is indistinguishable from an undeclared one, so the runtime tracks
"declared" explicitly rather than inferring it from the numbers. A profile that
genuinely costs nothing per token (`ollama`) declares a free card and is accepted; a
paid provider that was never priced is rejected instead of silently reporting `$0`.
`--allow-unpriced` is the explicit escape hatch for a smoke run.

## 2. Cost model

Cost is computed per model call, not per run, so cached and uncached input are priced
separately:

```text
cost = (billable_input_tokens × input_rate
      + cached_input_tokens  × cached_rate
      + output_tokens        × output_rate) / 1e6

billable_input_tokens = input_tokens − cached_input_tokens
```

`cached_input_tokens` is a *subset* of `input_tokens`, matching how OpenAI-compatible
providers report prompt-cache hits in `usage.prompt_tokens_details.cached_tokens`. When
no cached rate is declared it falls back to the uncached rate, which reproduces the
pre-cache cost model exactly.

Two derived numbers matter more than the total:

- **cost per successful run** — `total_cost / successful_runs`. A cheap variant that
  fails more is not cheap.
- **cached input share** — the fraction of prompt tokens served from cache. This is the
  number that decides whether a long-context strategy is affordable in production.

## 3. Latency model

Every run reports a three-way split, because a single "average latency" is dominated by
the network and tells you nothing about the harness:

| Component | Source | What it means |
| --- | --- | --- |
| `model_ms` | summed `model_call_completed.duration_ms` | waiting on the provider, including retries |
| `tool_ms` | summed `tool_call_completed.duration_ms` | subprocess and filesystem work |
| `harness_ms` | `total_ms − model_ms − tool_ms` | the runtime's own overhead |

`harness_ms` is the only component a harness author can optimize, and it is the residual
rather than a measured quantity, so it is reported alongside `model_share` and
`overhead_share` instead of standing alone.

An observation worth recording: on the scripted suites the model is effectively free
(~0.1 ms per call), so `harness_ms` and `tool_ms` dominate the end-to-end number. That is
a harness-side cost which is invisible unless the split exists, and it is irrelevant to a
real-provider run where `model_ms` is orders of magnitude larger.

## 4. Repetition protocol

A single real-model sample cannot be distinguished from a lucky one.

```bash
mini-dsh benchmark benchmarks/repo/manifest.json \
  --model-profile deepseek --model deepseek-chat \
  --repeat 3 \
  --input-usd-per-million <in> --output-usd-per-million <out> \
  --allow-write --allow-shell --allow-command python \
  --verify-command python \
  --output benchmark-results/real-repo.json
```

- `--temperature 0` (the default) and an identical budget for every run.
- `--repeat 3..5`; repeats are **interleaved per task**, not batched per sweep, so a
  transient provider incident lands inside one task's sample instead of correlating
  across the whole suite.
- The report then carries `pass_at_k` (task solved at least once), `repeat_variance`
  (`success_spread`, `token_spread`), and one row per run.

Fairness rules that the runner enforces rather than documents: identical task fixtures,
identical budgets, identical isolation, one changed variable per ablation, and success
decided by an executable criterion — never by the model's own claim.

## 5. Which suite to use

| Suite | Tasks | Model | Purpose |
| --- | --- | --- | --- |
| `smoke` | 1 | scripted | end-to-end wiring self-check |
| `core` | 25 | scripted | harness correctness on small, regular tasks |
| `stress` | 3 | scripted | long history, large output, denied actions |
| `repo` | 6 | **provider** | cross-file reasoning, localisation, recovery |

The `repo` suite is the real-model suite. It has no response scripts at all, spans at
least two modules per task, and several fixtures are built so the first plausible edit
makes a *different* test fail — which is what forces multi-round error recovery instead
of a single lucky patch.

## 6. Reading the results

The report separates two questions that a blended success rate merges:

```text
## Outcome by layer
| Layer       | Runs | Meaning                                            |
| success     |   18 | finished and the success criterion passed          |
| harness     |    3 | the runtime correctly rejected or bounded the agent|
| agent       |    9 | the model had a working harness and still failed   |
| environment |    1 | the model provider failed                          |
```

A `harness`-layer failure is evidence the runtime worked. An `agent`-layer failure is
the interesting one. Quote the split, not the sum.

## 7. Provenance of the quoted numbers

Every number in the README is reproducible from a committed manifest plus a stated rate
card. When quoting one, be able to answer all six questions:

| Number | Workload | Baseline | Variant | Metric | Runs | Model |
| --- | --- | --- | --- | --- | --- | --- |
| `3.38×` cost | `stress` (3 tasks) | `bounded_output` | `unbounded_output` | `total_cost_usd` | 1 per task | scripted |
| `+40%` tokens | `stress` | `sliding_window` | `repo_retrieval` | `average_tokens` | 1 per task | scripted |
| `−27%` tokens | `stress` | `repo_retrieval` | `symbol_retrieval` | `average_tokens` | 1 per task | scripted |

All three are **scripted-model** results and must be labelled as such. They are
statements about the harness — how output bounding and retrieval change the context the
model is handed — and they are exactly reproducible. Re-running the same ablations
against a real provider is the next step, and the outcome may differ, which is the
point: the ablation harness is what makes that difference measurable.

## 8. Reproducing a real-model experiment

```bash
uv sync --all-groups
uv run mini-dsh doctor

export DEEPSEEK_API_KEY=...
export MINIDSH_MODEL=deepseek-chat

# One task, full trace, step-by-step replay afterwards.
uv run mini-dsh run "run the tests and fix the failure" \
  --workspace /path/to/repo --model-profile deepseek \
  --input-usd-per-million <in> --output-usd-per-million <out> \
  --allow-write --allow-shell --allow-command python --trace /tmp/run.jsonl
uv run mini-dsh trace replay /tmp/run.jsonl

# The whole real-model suite, three samples per task.
uv run mini-dsh benchmark benchmarks/repo/manifest.json \
  --model-profile deepseek --repeat 3 \
  --input-usd-per-million <in> --output-usd-per-million <out> \
  --allow-write --allow-shell --allow-command python \
  --verify-command python --output benchmark-results/real-repo.json

# Trade-off analysis of the saved report, without re-running anything.
uv run mini-dsh report benchmark-results/real-repo.json
```

The `report` command is intentionally offline: it reads a saved JSON report and
regenerates the Markdown and Pareto sections, so a report can be re-rendered and
audited long after the provider call has been paid for.

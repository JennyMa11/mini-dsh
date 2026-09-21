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
| `deepseek` (`deepseek-flash`) | `api.deepseek.com/chat/completions` | `DEEPSEEK_API_KEY` | must be declared |
| `deepseek-v4-pro` | `api.deepseek.com/chat/completions` | `DEEPSEEK_API_KEY` | must be declared |
| `openai` | `api.openai.com/v1/chat/completions` | `OPENAI_API_KEY` | must be declared |
| `ollama` | `127.0.0.1:11434/v1/chat/completions` | none required | declared as free |

```bash
export DEEPSEEK_API_KEY=...
export MINIDSH_MODEL=deepseek-flash          # optional; overrides the profile default
```

`--endpoint`, `--model` and `--api-key-env` override the profile field by field, and
`MINIDSH_MODEL_PROFILE` selects a profile without a flag.

### Thinking mode is off by default, and that is a reproducibility decision

DeepSeek enables chain-of-thought by default upstream. The harness turns it off,
because **thinking mode makes the API ignore `temperature`** — the parameter is
accepted and silently has no effect. Since the whole repetition protocol rests on
`temperature=0`, leaving thinking on would produce a run that *claims* to be pinned
and is not. `describe()` records `temperature_effective` so a report says which of the
two situations it is in.

Turning it back on with `--thinking` is supported, and the harness then echoes
`reasoning_content` back on every request that carries `tools`; without that the API
answers **400**. That makes thinking a legitimate Harness variable — it changes the
cost per step, the number of steps, and the accuracy — but it is not a free one, and a
comparison between a thinking run and a non-thinking run is a comparison of two
different experimental conditions, not of two harnesses.

### Peak/off-peak pricing is why a rate card cannot be pinned

DeepSeek bills on a peak/off-peak schedule: off-peak is half of peak, and peak hours
are 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday, excluding Chinese public
holidays. A single rate card therefore cannot describe a run that crosses a tariff
boundary, which is a second, independent reason not to hard-code one. **A cost number
is only meaningful together with the tariff that applied to it** — say which one you
used when you quote it.

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
  --model-profile deepseek --model deepseek-flash \
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

The report separates two questions that a blended success rate merges. An
illustrative split, from a suite where some runs genuinely failed:

```text
## Outcome by layer
| Layer       | Runs | Meaning                                            |
| success     |   18 | finished and the success criterion passed          |
| harness     |    3 | the runtime correctly rejected or bounded the agent|
| agent       |    9 | the model had a working harness and still failed   |
| environment |    1 | the model provider failed                          |
```

A `harness`-layer failure is evidence the runtime worked. An `agent`-layer failure is
the interesting one. Quote the split, not the sum. Section 9 shows what a real run
produced.

## 7. Provenance of the quoted numbers

Every number in the README is reproducible from a committed manifest plus a stated rate
card. When quoting one, be able to answer all six questions:

| Number | Workload | Baseline | Variant | Metric | Runs | Model |
| --- | --- | --- | --- | --- | --- | --- |
| `3.38×` cost | `stress` (3 tasks) | `bounded_output` | `unbounded_output` | `total_cost_usd` | 1 per task | scripted |
| `+40%` tokens | `stress` | `sliding_window` | `repo_retrieval` | `average_tokens` | 1 per task | scripted |
| `−27%` tokens | `stress` | `repo_retrieval` | `symbol_retrieval` | `average_tokens` | 1 per task | scripted |
| `100%` success | `repo` (6 tasks) | — | — | `success_rate` | 3 per task | **`deepseek-flash`** |
| `$0.000937` per success | `repo` | — | — | `cost_per_success_usd` | 3 per task | **`deepseek-flash`** |
| `86%` cached input | `repo` | — | — | `cached_input_share` | 3 per task | **`deepseek-flash`** |
| `0.26%` harness share | `repo` | — | — | `harness_ms / latency_ms` | 3 per task | **`deepseek-flash`** |

The first three are **scripted-model** results and must be labelled as such. They are
statements about the harness — how output bounding and retrieval change the context the
model is handed — and they are exactly reproducible.

The last four are the **real-provider** result, and their provenance is the archived
report itself: `docs/results/repo-real.md` carries the profile, the endpoint, the
thinking flag, `temperature_effective`, and the tariff, so the numbers cannot be quoted
without them. The remaining step is to re-run the *ablations* against a real provider:
the outcome may differ from the scripted one, which is the point — the ablation harness
is what makes that difference measurable.

## 8. Reproducing a real-model experiment

```bash
uv sync --all-groups
uv run mini-dsh doctor

export DEEPSEEK_API_KEY=...
export MINIDSH_MODEL=deepseek-flash

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

## 9. The first real-model run, and what it does not show

`repo` suite, 6 tasks x 3 interleaved repeats, `deepseek-flash`, thinking off,
`temperature=0`, off-peak tariff `$0.15` / `$0.6` / `$0.003` per 1M tokens
(input / output / cache hit). Archived as [`results/repo-real.md`](results/repo-real.md),
with the machine-readable aggregate in `results/summary.json` under `real_model`.

```text
Success rate          100%  (18/18)
Pass@k                100%
Outcome by layer      success 18 / harness 0 / agent 0 / environment 0
repeat_variance       success spread 0.00, token spread 15%
Average steps         6.61
Average tokens        19,132
Cached input share    86%
Average latency       6,410.1 ms = model 6,290.6 + tool 103.1 + harness 16.4
Total cost            $0.016865        Cost per success  $0.000937
Unsafe actions blocked 20
```

### What it does establish

- **The multi-step tool loop works against a real provider.** This was not a
  formality: thinking is on upstream by default, thinking mode makes the API ignore
  `temperature`, and a request carrying `tools` is rejected with a **400** unless every
  earlier turn's `reasoning_content` is echoed back. The adapter did neither, so the
  default configuration would have failed on the first tool round. The fix is what makes
  these 18 runs possible, and it was found by running the suite rather than by reading
  the docs.
- **The harness is not the bottleneck.** `harness_ms` is 16.4 ms of a 6,410.1 ms run —
  **0.26%**. The residual is the only part a harness author controls, and on a real
  provider it is two orders of magnitude smaller than the network. On the scripted
  suites the same residual dominated the total, which is precisely why the split exists.
- **Long-context economics, measured.** 86% of input tokens were served from the prompt
  cache. That single number decides whether a retrieval strategy is affordable in
  production, and it cannot be obtained from a scripted run.
- **The successes are not test edits.** Across all 18 traces, every write landed on a
  source file (`shop/pricing.py`, `textkit/normalize.py` + `textkit/slugify.py`,
  `durations/formatting.py` + `durations/parser.py`, `ledger/entries.py`,
  `billing/discounts.py`, `service/settings.py`), one per repeat, and **zero** writes
  touched any test file. The two-task fixtures that force multi-round recovery
  (`repo-error-recovery`, `repo-feature-with-tests`) both show the second file being
  edited, so the recovery path was exercised rather than skipped.

### What it does not show

- **Not a capability claim.** 6 tasks x 3 runs is a very small sample, and the tasks were
  written by the same person who wrote the harness. 100% here rules out wholesale
  failure; it says nothing about success rates on real repositories. The `repo` suite is
  the *minimum credible source*, not an independent evaluation — see the SWE-bench
  roadmap in [`technical-highlights.md`](technical-highlights.md).
- **`unsafe_blocked: 20` is a configuration artefact, not a security finding.** All 20
  are `ls` (18) and `find` (2) failing the command allowlist, because the run only
  admitted `python`. The agent switched to `read_file` and `search` and still passed
  everything, so the taxonomy files them under `success` — the denials are evidence the
  policy fired, and also a note that the agent worked without the shell commands it
  reached for first.
- **A single sample was not enough, and a re-run destroys the previous evidence.**
  Reports and traces are written in place: re-running the same `--output` overwrites the
  JSON, the Markdown, and every trace under `<output>-artifacts/`. Archive into
  `docs/results/` *before* re-running. An earlier full run of this same suite, made
  before reports recorded their own conditions, also finished 18/18, but its raw
  artefacts were overwritten and are therefore deliberately not quoted here.


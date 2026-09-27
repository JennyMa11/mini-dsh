# Real-model evaluation

The current Session journal and runtime composition were checked on the clean implementation
commit `2e7d351` with one DeepSeek sample per task: repo 6/6 and hard 4/4, at a computed
total cost of $0.01713647. This is an integration regression, not a stable success-rate
estimate; conditions and per-task results are in the
[2026-09-27 regression report](results/real-regression-2026-09-27.md).

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


## 10. Real-model ablations, and the harness bug that had to be fixed first

The scripted ablations prove that the harness *can* separate strategies on cost; they
cannot prove that any strategy is more *accurate*, because a scripted model scores 100%
by construction. Moving the same single-variable ablation onto a real provider is
therefore the only way to learn anything new — and the first attempt produced a result
that looked like a finding and was not.

### The first attempt measured a bug

Six Context variants x 3 tasks x 3 repeats, `deepseek-flash`. Four of them scored
between 0% and 22%, and **every** failure was classified `provider_error`, which lands
in the `environment` layer — read at face value, the provider had been unreliable.
Reading the traces gave `ModelAdapterError: provider returned HTTP 400`, and the shape
of the failures matched the base strategy exactly: the two variants built on full
history passed 9/9, and all four truncating variants failed.

The cause was in the harness. An OpenAI-compatible provider rejects a request whose
`tool` message has no preceding assistant message declaring the matching `tool_call_id`,
and `sliding_window` / `summary_compaction` sliced history at a raw index — keeping a
tool result while dropping the assistant turn that declared it. Execution order
(`full_history, sliding_window, summary_compaction, repo_retrieval, retrieval_summary,
symbol_retrieval`) ruled out a transient outage, since the clean `repo_retrieval` sat
between failing variants.

**This defect is structurally invisible to a scripted model**, which does not validate
the request it is handed. It could not have been found by running the deterministic
suites more often or more carefully; it needed a real provider to surface, and then it
needed the error body to be readable rather than discarded. Both halves are fixed in
`0.3.1`: `_window_start()` grows a window back to the assistant message that opened the
round, and `_error_detail()` carries the provider's own redacted words into the failure
reason. The invariant is written down in
[`context-engineering.md`](context-engineering.md) §3.1.

### The corrected Context ablation

Six variants x 3 tasks x 3 repeats, `deepseek-flash`, thinking off, off-peak tariff
`$0.15` / `$0.6` / `$0.003` per 1M tokens. Archived as
[`results/stress-real-context-ablation.md`](results/stress-real-context-ablation.md).

| Variant | Success | Tokens | Steps | Cache share | Total cost | Cost/success |
| --- | --- | --- | --- | --- | --- | --- |
| `full_history` | 100% | 29,315 | 8.33 | 92% | $0.011553 | $0.001284 |
| `sliding_window` | 100% | 26,096 | 8.56 | 85% | $0.012579 | $0.001398 |
| `summary_compaction` | 100% | 26,073 | 8.22 | 83% | $0.013322 | $0.001480 |
| `repo_retrieval` | 100% | 17,767 | 6.56 | 80% | $0.011184 | $0.001243 |
| `retrieval_summary` | 100% | 16,747 | 6.33 | 75% | $0.011803 | $0.001311 |
| `symbol_retrieval` | 100% | 29,331 | 7.56 | 77% | $0.016193 | $0.001799 |

- **Success is 100% for all six, so that column still carries no signal.** 54 runs, every
  failure layer `success 9`. The `stress` suite is too easy for this model to separate
  the strategies on accuracy. **This is a cost result, not a capability result** — the
  same limitation as the scripted ablation, now stated on a real provider.
- **Fewer tokens is not cheaper, and under prompt caching the ordering inverts.**
  `sliding_window` (26,096) and `summary_compaction` (26,073) both use ~11% fewer tokens
  than `full_history` (29,315) and both cost *more* ($0.012579 / $0.013322 vs
  $0.011553). The mechanism is the cache: a hit bills at $0.003 against $0.15 uncached,
  a 50x difference, and a truncating strategy rewrites its prefix every step while
  `full_history` only appends — hence 92% cache share for the latter and 75–85% for the
  rest. **Token count is a systematically wrong proxy for cost on a cached provider.**
- **`repo_retrieval` is the cost winner** ($0.011184, the only variant undominated on
  cost) with near-fewest steps (6.56): it is the best balance between injecting a little
  more context and keeping the cacheable prefix intact.
- **`retrieval_summary` wins on tokens, steps and latency** (16,747 / 6.33 / 6,826 ms)
  yet costs more than `repo_retrieval`, because its summary message is recomputed every
  step and its cache share is the lowest of the six.
- **Harness overhead is 18.9–27.1 ms, 0.2–0.4% of wall clock**, consistent across
  variants — the spread in this table is not harness-side.

### The corrected tool-output ablation

Three variants x 3 tasks x 3 repeats, same conditions. Archived as
[`results/stress-real-tool_output-ablation.md`](results/stress-real-tool_output-ablation.md).
All 27 runs passed, so this is also a cost result.

| Variant | Success | Tokens | Total cost | Cost/success |
| --- | --- | --- | --- | --- |
| `unbounded_output` (limit 200K) | 100% | 32,687 | $0.011849 | $0.001317 |
| `bounded_output` (limit 20K) | 100% | 31,986 | $0.011743 | $0.001305 |
| `bounded_output_artifact` (limit 20K + offload) | 100% | 30,523 | $0.011148 | $0.001239 |

- **The lever that works is offloading, not the limit.** `bounded_output_artifact` is
  undominated on tokens, cost and steps: 6.6% fewer tokens and 5.9% lower cost than no
  limit at all, with success unchanged. Lowering the limit alone (200K to 20K) buys only
  2.1%. Capping output is cheap to implement; persisting the overflow is what pays.

### What these two ablations do not show

- **No accuracy claim.** Both suites are fully saturated at 100%, so neither can rank
  strategies by correctness. A harder task set is a prerequisite for that, not a
  refinement of this one.
- **No independent replication.** 3 tasks x 3 repeats per variant is enough to make a
  cost ordering visible and not enough to make it robust. The orderings above are the
  first measurement, not an established result.

## 11. The hard suite: the first result that is not 100%

Every suite up to this point saturated. `core`, `stress` and `repo` all verify by running
tests inside the workspace the agent could read and edit, so a success rate measured
compliance with the visible suite rather than correctness against a specification, and a
model that greened the tests scored full marks whether or not it honoured the contract.
The `hard` suite moves the criterion out of the agent's reach (see
[`benchmark.md`](benchmark.md) for `verification_overlay`), and the same model on the same
harness finally scored below 100%.

Archived as [`results/hard.md`](results/hard.md).

### The run

4 tasks x 5 repeats = 20 runs, `deepseek-flash`, thinking off, off-peak tariff
`$0.15` / `$0.6` / `$0.003` per 1M tokens.

| Task | Success | Steps | Tokens | Cost/success |
| --- | --- | --- | --- | --- |
| `hard-config-precedence` | 100% (5/5) | 7.80 | 23,488 | $0.001579 |
| `hard-csv-records` | 100% (5/5) | 9.40 | 47,075 | $0.002607 |
| `hard-interval-merge` | 80% (4/5) | 9.20 | 56,818 | $0.003702 |
| `hard-token-bucket` | 100% (5/5) | 14.00 | 72,819 | $0.003085 |

Aggregate: 95% (19/20), `pass@k` 100%, 10.1 steps, 50,050 tokens, 12.85 tool calls, 92.7%
cache share, $0.002693 per success, 34 unsafe actions blocked, 0 permission denials.

### The one failure is a token-budget exhaustion, and the layer name misleads

`hard-interval-merge` run 2 ended at step 17 of a 24-step budget with **all 21 tool calls
succeeded**, having spent 151,854 tokens against a 150,000 ceiling. The trace shows what it
was doing: it patched the implementation, then verified with a series of self-written
`python -c` probes — including randomised input — and the final step is it returning to
`unittest` to re-check. The budget cut it off one step before the check that would have
decided the run.

Two things follow, and the second is the important one.

**The binding constraint is tokens, not steps.** A reader who thinks in steps would look at
17/24 and conclude there was headroom. The step budget was never close; the cumulative
token budget was the wall, and cumulative tokens grow with every step because each request
re-sends the history.

**`harness` is an exoneration label, not a fault label.** The layer split is
`success 19 / harness 1 / agent 0 / environment 0`, and the report's own gloss for
`harness` is "the runtime correctly bounded the agent" — which is true, the budget was
enforced as specified. But the taxonomy answers *is the runtime exonerated?*, not *did the
model fail?*, and here those two questions have opposite answers. Read as capability, this
is a run where the model iterated without converging. **`agent: 0` must not be read as "the
model never failed"**; it means no failure was attributable to a broken harness or a dead
provider.

This is the third time a real-model run has surfaced a reporting defect that a scripted
model cannot reach — the first was the truncation bug that produced opaque 400s, the second
was `ModelAdapterError` being filed as environmental regardless of what it said, and this
one is a label whose meaning is narrower than the question it gets asked. The first two
were fixed in code; this one is left as a documented ambiguity, because the taxonomy's
definition is deliberate and changing it would redefine the layer rather than correct it.

### What the suite shows, and what it does not

- **Cost is an order of magnitude above `repo`**: $0.002693 per success against $0.000937,
  and 50,050 tokens against 19,132. That is the expected shape — `repo` fixes small
  cross-file bugs, `hard` implements a whole module specification.
- **The most expensive task does not fail.** `hard-token-bucket` is highest on both tokens
  (72,819) and steps (14.00) and passes 5/5. On this task set "expensive" and "hard" are
  not the same thing, which is the same warning as the ablation's cost inversion: no single
  scalar tracks difficulty.
- **Only one task has spread.** `hard-interval-merge` varies (success spread 0.25, token
  spread 64%); the other three are 5/5. The suite therefore currently measures sampling
  variance within a task, not a difficulty ordering across tasks.
- **34 blocked actions, 31 of them `find`.** The `hard` manifest allows only `python`, the
  same allowlist `repo` uses, so the two runs stay comparable — but the agent repeatedly
  reaches for `find` to explore and is refused. It works around the refusal with
  `python -c` and still passes. Worth stating because it is a real cost of a minimal
  allowlist, and because the allowlist's practical effect is limited: `python` can already
  do anything the denied commands could.
- **20 runs cannot rank four tasks or strategies.** `pass@k` is still 100% — every task
  passed at least once — so the suite cannot yet separate Context strategies on accuracy.
  What it does establish is that the criterion is no longer saturated, which is the
  precondition for that experiment rather than the experiment itself.

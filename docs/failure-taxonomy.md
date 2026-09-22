# Failure taxonomy

A single success rate answers neither of the two questions a harness author actually
has. This taxonomy splits a terminal outcome into nine categories and then folds those
categories into four **layers**, so that "the runtime worked" and "the model worked" stop
being the same number.

## The two questions

| Question | Evidence | Where it shows up |
| --- | --- | --- |
| Did the runtime do what it promised? | the runtime recorded a structured rejection or a hard stop | `harness` layer |
| Given a correct runtime, did the model produce a working change? | the criterion still fails, or the agent looped | `agent` layer |

A `permission_denied` failure is **evidence the harness is correct**: the agent asked for
something it was not granted and the policy layer said no. Blending that into one
"success rate" makes a healthy runtime look broken.

### A layer answers "is the runtime exonerated?", not "did the model fail?"

Those two questions have the same answer everywhere except `budget_exhausted`, and there
they come apart. A run stopped by a budget was bounded exactly as specified, so the
runtime is exonerated — but read as capability it is a run that **did not converge**. The
`hard` suite produced precisely this case: a task ended at step 17 of 24 with all 21 tool
calls succeeded, having spent 151,854 tokens against a 150,000 ceiling, and was filed
`harness`.

So a `harness` count is not a claim that the model did nothing wrong, and **`agent: 0` must
not be read as "the model never failed"** — only as "no failure was attributable to a
broken runtime or a dead provider". When a suite reports a non-zero `harness` count, read
the categories behind it: `permission_denied`, `invalid_tool_call` and `tool_error` are
statements about the runtime's behaviour, while `budget_exhausted` is a statement about the
budget and says nothing either way about the model's capability.

## Categories

| Category | Layer | Meaning |
| --- | --- | --- |
| `none` | success | the run finished and the executable criterion passed |
| `model_reasoning_failure` | agent | the run ended without a verified success and no more specific evidence exists |
| `context_miss` | agent | the context strategy dropped history, the agent never re-read the repository, and the change was wrong |
| `verification_failed` | agent | the agent inspected the repository, claimed completion, and the criterion still fails |
| `loop_stuck` | agent | the same tool call returned an identical result three times |
| `permission_denied` | harness | the agent needed a capability the run never granted, and never worked around it |
| `invalid_tool_call` | harness | the model issued a call the runtime could not accept (unknown tool, bad arguments) |
| `tool_error` | harness | a tool the agent depended on failed and was never followed by a success of that tool |
| `budget_exhausted` | harness | a step, token, tool-call, or wall-clock budget stopped the run |
| `provider_error` | environment | the model provider failed and the run could not continue |

`provider_error` is an addition to the original eight-category list. Classifying a
provider outage as a `model_reasoning_failure` would corrupt the capability analysis in
the most flattering direction possible, so it gets its own layer.

## Decision order

The classifier tests unambiguous structured facts before inferring anything about the
model. This matters because a run that hit a denial *and* produced a wrong change has two
plausible stories, and only one of them is supported by hard evidence.

```text
1. finished and verified          → none
2. cancelled by the caller        → none
3. status == budget_exhausted     → budget_exhausted
4. trace records a provider error → provider_error
5. identical tool result ×3       → loop_stuck
6. unrecovered permission denial  → permission_denied
7. invalid tool call recorded     → invalid_tool_call
8. tool failed and never succeeded→ tool_error
9. adapter reported an unusable   → model_reasoning_failure
   model response
10. status == failed               → model_reasoning_failure
11. finished but criterion failed → context_miss  (if history was dropped and nothing was read)
                                  → verification_failed  (otherwise)
```

Two rules are worth calling out:

- **A denial the agent worked around is not the cause.** If the denied tool was later
  executed successfully, the denial is history, not the diagnosis. Without this rule a
  healthy harness looks broken on every run that probes a boundary first.
- **`context_miss` requires evidence of both halves.** History must actually have been
  omitted (`omitted_messages > 0`) *and* the agent must never have called a read/search/
  git tool. Dropping history alone is not a diagnosis; it is a configuration.

Step 4 is decided by the **error code** the adapter attached, not by the exception class
name. `ModelAdapterError` covers both a provider outage and a model that emitted
unparseable output, and only the code separates them: `bad_response` and
`invalid_tool_arguments` mean an answer arrived and was unusable, so they are deferred to
step 9 and land in the `agent` layer; every other code is environmental. Filing a malformed
generation as `provider_error` would inflate the layer that is supposed to mean "not the
model's fault" — the same failure mode, in the same direction, as the `provider_error`
category was created to prevent.

## Diagnosing a run

Every terminal state produces a `FailureDiagnosis` carrying the category, a human
reason, and the evidence that justified it:

```json
{
  "category": "context_miss",
  "reason": "the context strategy dropped history and the agent never re-read the repository",
  "evidence": ["verifier_exit_code:1", "omitted_messages:7", "inspected_repository:False"],
  "layer": "agent"
}
```

The evidence list is the point: a diagnosis that cannot show its work is not auditable.

## Working with traces

The diagnosis is derived from the JSONL trace, never stored in it. The trace is the fact
source; every view in this document is regenerable from it.

```bash
# Reconstruct Context → Model → Tool Call → Observation → State, step by step.
mini-dsh trace replay benchmark-results/real-repo-artifacts/traces/repo-cross-file-bug.jsonl

# Compare two variants of the same task and find the first divergent step.
mini-dsh trace diff \
  benchmark-results/core-context-full_history/traces/multi-file-discount.jsonl \
  benchmark-results/core-context-repo_retrieval/traces/multi-file-discount.jsonl \
  --left-label full_history --right-label repo_retrieval
```

The diff output answers three questions at once — which step's context differed, by how
many tokens, and why the tool calls forked:

```text
- Tokens: 1153 vs 1909 (+756, +65.6%)
First behavioural divergence at **step 1**: context strategy diverged: full_history vs repo_retrieval+full_history

| Step | Context Δ | Token Δ | Left                        | Right                              | Divergence |
| 1    | +189      | +189    | context=full_history ...    | context=repo_retrieval+full_history| ...        |
```

## Failure analysis workflow

1. Run the suite against a real provider with `--repeat 3`.
2. Read `failure_layers` first, but read the categories behind a non-zero `harness` count
   rather than the count itself. `permission_denied`, `invalid_tool_call` and `tool_error`
   are the runtime reporting its own behaviour, and a `permission_denied` the agent worked
   around is not a defect at all. `budget_exhausted` is not a harness bug either — it means
   the run was bounded, and whether that bound was *right* is a question about the task's
   declared budget, not about the runtime. None of the four is a capability signal.
3. For the `agent`-layer failures, sort by category. `context_miss` and `loop_stuck` are
   harness-configurable; `verification_failed` and a `model_reasoning_failure` from an
   unusable response are genuine capability signals.
4. Pick 5–10 tasks from the largest agent-layer category and replay each trace.
5. For each one, locate the step where the trajectory went wrong and ask whether a
   different context strategy, tool, or budget would have changed it. Only then is a new
   feature justified.

Step 4 is deliberately manual. Automating it produces a list of failures; reading the
traces produces a reason to change something.

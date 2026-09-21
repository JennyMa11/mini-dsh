# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full_history | full | off | detailed | 100% | 100% | 8.33 | 29314.8 | 0.011553 | 0.001284 | 0% | 0 | 30 |
| sliding_window | sliding | off | detailed | 100% | 100% | 8.56 | 26095.6 | 0.012579 | 0.001398 | 0% | 0 | 30 |
| summary_compaction | summary | off | detailed | 100% | 100% | 8.22 | 26073.2 | 0.013322 | 0.001480 | 0% | 0 | 28 |
| repo_retrieval | retrieval | off | detailed | 100% | 100% | 6.56 | 17767.4 | 0.011184 | 0.001243 | 0% | 0 | 27 |
| retrieval_summary | retrieval_summary | off | detailed | 100% | 100% | 6.33 | 16747.2 | 0.011803 | 0.001311 | 0% | 0 | 24 |
| symbol_retrieval | symbol | off | detailed | 100% | 100% | 7.56 | 29331.2 | 0.016193 | 0.001799 | 0% | 0 | 27 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| retrieval_summary | 100% | 1.675e+04 | yes |
| repo_retrieval | 100% | 1.777e+04 | no |
| summary_compaction | 100% | 2.607e+04 | no |
| sliding_window | 100% | 2.61e+04 | no |
| full_history | 100% | 2.931e+04 | no |
| symbol_retrieval | 100% | 2.933e+04 | no |

Dominated variants:

- `full_history` is dominated by `retrieval_summary`: +0% success at +1.257e+04 Tokens.
- `sliding_window` is dominated by `retrieval_summary`: +0% success at +9348 Tokens.
- `summary_compaction` is dominated by `retrieval_summary`: +0% success at +9326 Tokens.
- `repo_retrieval` is dominated by `retrieval_summary`: +0% success at +1020 Tokens.
- `symbol_retrieval` is dominated by `retrieval_summary`: +0% success at +1.258e+04 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| repo_retrieval | 100% | 0.01118 | yes |
| full_history | 100% | 0.01155 | no |
| retrieval_summary | 100% | 0.0118 | no |
| sliding_window | 100% | 0.01258 | no |
| summary_compaction | 100% | 0.01332 | no |
| symbol_retrieval | 100% | 0.01619 | no |

Dominated variants:

- `full_history` is dominated by `repo_retrieval`: +0% success at +0.0003686 Cost (USD).
- `sliding_window` is dominated by `repo_retrieval`: +0% success at +0.001394 Cost (USD).
- `summary_compaction` is dominated by `repo_retrieval`: +0% success at +0.002137 Cost (USD).
- `retrieval_summary` is dominated by `repo_retrieval`: +0% success at +0.0006189 Cost (USD).
- `symbol_retrieval` is dominated by `repo_retrieval`: +0% success at +0.005008 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| retrieval_summary | 100% | 6.333 | yes |
| repo_retrieval | 100% | 6.556 | no |
| symbol_retrieval | 100% | 7.556 | no |
| summary_compaction | 100% | 8.222 | no |
| full_history | 100% | 8.333 | no |
| sliding_window | 100% | 8.556 | no |

Dominated variants:

- `full_history` is dominated by `retrieval_summary`: +0% success at +2 Steps.
- `sliding_window` is dominated by `retrieval_summary`: +0% success at +2.222 Steps.
- `summary_compaction` is dominated by `retrieval_summary`: +0% success at +1.889 Steps.
- `repo_retrieval` is dominated by `retrieval_summary`: +0% success at +0.2222 Steps.
- `symbol_retrieval` is dominated by `retrieval_summary`: +0% success at +1.222 Steps.


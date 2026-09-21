# Ablation report: mini-dsh-core

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full_history | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| sliding_window | sliding | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| summary_compaction | summary | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| repo_retrieval | retrieval | off | detailed | 100% | 100% | 4.00 | 1355.1 | 0.109938 | 0.004398 | 0% | 0 | 0 |
| retrieval_summary | retrieval_summary | off | detailed | 100% | 100% | 4.00 | 1355.1 | 0.109938 | 0.004398 | 0% | 0 | 0 |
| symbol_retrieval | symbol | off | detailed | 100% | 100% | 4.00 | 1046.1 | 0.086760 | 0.003470 | 0% | 0 | 0 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| full_history | 100% | 749.9 | yes |
| sliding_window | 100% | 749.9 | yes |
| summary_compaction | 100% | 749.9 | yes |
| symbol_retrieval | 100% | 1046 | no |
| repo_retrieval | 100% | 1355 | no |
| retrieval_summary | 100% | 1355 | no |

Dominated variants:

- `repo_retrieval` is dominated by `full_history`: +0% success at +605.2 Tokens.
- `retrieval_summary` is dominated by `full_history`: +0% success at +605.2 Tokens.
- `symbol_retrieval` is dominated by `full_history`: +0% success at +296.2 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| full_history | 100% | 0.06455 | yes |
| sliding_window | 100% | 0.06455 | yes |
| summary_compaction | 100% | 0.06455 | yes |
| symbol_retrieval | 100% | 0.08676 | no |
| repo_retrieval | 100% | 0.1099 | no |
| retrieval_summary | 100% | 0.1099 | no |

Dominated variants:

- `repo_retrieval` is dominated by `full_history`: +0% success at +0.04539 Cost (USD).
- `retrieval_summary` is dominated by `full_history`: +0% success at +0.04539 Cost (USD).
- `symbol_retrieval` is dominated by `full_history`: +0% success at +0.02221 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| full_history | 100% | 4 | yes |
| sliding_window | 100% | 4 | yes |
| summary_compaction | 100% | 4 | yes |
| repo_retrieval | 100% | 4 | yes |
| retrieval_summary | 100% | 4 | yes |
| symbol_retrieval | 100% | 4 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


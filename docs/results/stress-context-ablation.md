# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full_history | full | off | detailed | 100% | 100% | 10.00 | 9534.0 | 0.095022 | 0.031674 | 0% | 1 | 4 |
| sliding_window | sliding | off | detailed | 100% | 100% | 10.00 | 8981.0 | 0.090045 | 0.030015 | 0% | 1 | 4 |
| summary_compaction | summary | off | detailed | 100% | 100% | 10.00 | 9233.7 | 0.092319 | 0.030773 | 0% | 1 | 4 |
| repo_retrieval | retrieval | off | detailed | 100% | 100% | 10.00 | 12617.0 | 0.122769 | 0.040923 | 0% | 1 | 4 |
| retrieval_summary | retrieval_summary | off | detailed | 100% | 100% | 10.00 | 12317.0 | 0.120069 | 0.040023 | 0% | 1 | 4 |
| symbol_retrieval | symbol | off | detailed | 100% | 100% | 10.00 | 9233.7 | 0.092319 | 0.030773 | 0% | 1 | 4 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| sliding_window | 100% | 8981 | yes |
| summary_compaction | 100% | 9234 | no |
| symbol_retrieval | 100% | 9234 | no |
| full_history | 100% | 9534 | no |
| retrieval_summary | 100% | 1.232e+04 | no |
| repo_retrieval | 100% | 1.262e+04 | no |

Dominated variants:

- `full_history` is dominated by `sliding_window`: +0% success at +553 Tokens.
- `summary_compaction` is dominated by `sliding_window`: +0% success at +252.7 Tokens.
- `repo_retrieval` is dominated by `sliding_window`: +0% success at +3636 Tokens.
- `retrieval_summary` is dominated by `sliding_window`: +0% success at +3336 Tokens.
- `symbol_retrieval` is dominated by `sliding_window`: +0% success at +252.7 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| sliding_window | 100% | 0.09005 | yes |
| summary_compaction | 100% | 0.09232 | no |
| symbol_retrieval | 100% | 0.09232 | no |
| full_history | 100% | 0.09502 | no |
| retrieval_summary | 100% | 0.1201 | no |
| repo_retrieval | 100% | 0.1228 | no |

Dominated variants:

- `full_history` is dominated by `sliding_window`: +0% success at +0.004977 Cost (USD).
- `summary_compaction` is dominated by `sliding_window`: +0% success at +0.002274 Cost (USD).
- `repo_retrieval` is dominated by `sliding_window`: +0% success at +0.03272 Cost (USD).
- `retrieval_summary` is dominated by `sliding_window`: +0% success at +0.03002 Cost (USD).
- `symbol_retrieval` is dominated by `sliding_window`: +0% success at +0.002274 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| full_history | 100% | 10 | yes |
| sliding_window | 100% | 10 | yes |
| summary_compaction | 100% | 10 | yes |
| repo_retrieval | 100% | 10 | yes |
| retrieval_summary | 100% | 10 | yes |
| symbol_retrieval | 100% | 10 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


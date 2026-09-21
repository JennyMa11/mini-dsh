# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| no_retry | full | off | detailed | 100% | 100% | 10.00 | 9534.0 | 0.095022 | 0.031674 | 0% | 1 | 4 |
| single_retry | full | off | detailed | 100% | 100% | 10.00 | 9534.0 | 0.095022 | 0.031674 | 0% | 1 | 4 |
| basic_feedback | full | off | basic | 100% | 100% | 10.00 | 8622.7 | 0.086820 | 0.028940 | 0% | 1 | 4 |
| detailed_feedback | full | off | detailed | 100% | 100% | 10.00 | 9534.0 | 0.095022 | 0.031674 | 0% | 1 | 4 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| basic_feedback | 100% | 8623 | yes |
| no_retry | 100% | 9534 | no |
| single_retry | 100% | 9534 | no |
| detailed_feedback | 100% | 9534 | no |

Dominated variants:

- `no_retry` is dominated by `basic_feedback`: +0% success at +911.3 Tokens.
- `single_retry` is dominated by `basic_feedback`: +0% success at +911.3 Tokens.
- `detailed_feedback` is dominated by `basic_feedback`: +0% success at +911.3 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| basic_feedback | 100% | 0.08682 | yes |
| no_retry | 100% | 0.09502 | no |
| single_retry | 100% | 0.09502 | no |
| detailed_feedback | 100% | 0.09502 | no |

Dominated variants:

- `no_retry` is dominated by `basic_feedback`: +0% success at +0.008202 Cost (USD).
- `single_retry` is dominated by `basic_feedback`: +0% success at +0.008202 Cost (USD).
- `detailed_feedback` is dominated by `basic_feedback`: +0% success at +0.008202 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| no_retry | 100% | 10 | yes |
| single_retry | 100% | 10 | yes |
| basic_feedback | 100% | 10 | yes |
| detailed_feedback | 100% | 10 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


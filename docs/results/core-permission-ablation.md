# Ablation report: mini-dsh-core

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| no_retry | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| single_retry | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| basic_feedback | full | off | basic | 100% | 100% | 4.00 | 653.0 | 0.057279 | 0.002291 | 0% | 0 | 0 |
| detailed_feedback | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| basic_feedback | 100% | 653 | yes |
| no_retry | 100% | 749.9 | no |
| single_retry | 100% | 749.9 | no |
| detailed_feedback | 100% | 749.9 | no |

Dominated variants:

- `no_retry` is dominated by `basic_feedback`: +0% success at +96.88 Tokens.
- `single_retry` is dominated by `basic_feedback`: +0% success at +96.88 Tokens.
- `detailed_feedback` is dominated by `basic_feedback`: +0% success at +96.88 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| basic_feedback | 100% | 0.05728 | yes |
| no_retry | 100% | 0.06455 | no |
| single_retry | 100% | 0.06455 | no |
| detailed_feedback | 100% | 0.06455 | no |

Dominated variants:

- `no_retry` is dominated by `basic_feedback`: +0% success at +0.007266 Cost (USD).
- `single_retry` is dominated by `basic_feedback`: +0% success at +0.007266 Cost (USD).
- `detailed_feedback` is dominated by `basic_feedback`: +0% success at +0.007266 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| no_retry | 100% | 4 | yes |
| single_retry | 100% | 4 | yes |
| basic_feedback | 100% | 4 | yes |
| detailed_feedback | 100% | 4 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


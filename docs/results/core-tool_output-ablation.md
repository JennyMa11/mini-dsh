# Ablation report: mini-dsh-core

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| unbounded_output | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| bounded_output | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |
| bounded_output_artifact | full | off | detailed | 100% | 100% | 4.00 | 749.9 | 0.064545 | 0.002582 | 0% | 0 | 0 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| unbounded_output | 100% | 749.9 | yes |
| bounded_output | 100% | 749.9 | yes |
| bounded_output_artifact | 100% | 749.9 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| unbounded_output | 100% | 0.06455 | yes |
| bounded_output | 100% | 0.06455 | yes |
| bounded_output_artifact | 100% | 0.06455 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| unbounded_output | 100% | 4 | yes |
| bounded_output | 100% | 4 | yes |
| bounded_output_artifact | 100% | 4 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


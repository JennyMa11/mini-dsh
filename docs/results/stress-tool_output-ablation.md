# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| unbounded_output | full | off | detailed | 100% | 100% | 10.00 | 32136.0 | 0.298440 | 0.099480 | 0% | 1 | 4 |
| bounded_output | full | off | detailed | 100% | 100% | 10.00 | 9511.3 | 0.094818 | 0.031606 | 0% | 1 | 4 |
| bounded_output_artifact | full | off | detailed | 100% | 100% | 10.00 | 9534.0 | 0.095022 | 0.031674 | 0% | 1 | 4 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| bounded_output | 100% | 9511 | yes |
| bounded_output_artifact | 100% | 9534 | no |
| unbounded_output | 100% | 3.214e+04 | no |

Dominated variants:

- `unbounded_output` is dominated by `bounded_output`: +0% success at +2.262e+04 Tokens.
- `bounded_output_artifact` is dominated by `bounded_output`: +0% success at +22.67 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| bounded_output | 100% | 0.09482 | yes |
| bounded_output_artifact | 100% | 0.09502 | no |
| unbounded_output | 100% | 0.2984 | no |

Dominated variants:

- `unbounded_output` is dominated by `bounded_output`: +0% success at +0.2036 Cost (USD).
- `bounded_output_artifact` is dominated by `bounded_output`: +0% success at +0.000204 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| unbounded_output | 100% | 10 | yes |
| bounded_output | 100% | 10 | yes |
| bounded_output_artifact | 100% | 10 | yes |

All variants tie on this axis: the comparison cannot separate them, so it carries no signal.


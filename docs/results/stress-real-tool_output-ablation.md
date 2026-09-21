# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps | Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| unbounded_output | full | off | detailed | 100% | 100% | 9.11 | 32686.6 | 0.011849 | 0.001317 | 0% | 0 | 35 |
| bounded_output | full | off | detailed | 100% | 100% | 8.67 | 31985.9 | 0.011743 | 0.001305 | 0% | 0 | 30 |
| bounded_output_artifact | full | off | detailed | 100% | 100% | 8.67 | 30522.6 | 0.011148 | 0.001239 | 0% | 0 | 32 |

## Pareto trade-off analysis

### Success rate vs Tokens

| Variant | Success | Tokens | Undominated |
| --- | --- | --- | --- |
| bounded_output_artifact | 100% | 3.052e+04 | yes |
| bounded_output | 100% | 3.199e+04 | no |
| unbounded_output | 100% | 3.269e+04 | no |

Dominated variants:

- `unbounded_output` is dominated by `bounded_output_artifact`: +0% success at +2164 Tokens.
- `bounded_output` is dominated by `bounded_output_artifact`: +0% success at +1463 Tokens.

### Success rate vs Cost (USD)

| Variant | Success | Cost (USD) | Undominated |
| --- | --- | --- | --- |
| bounded_output_artifact | 100% | 0.01115 | yes |
| bounded_output | 100% | 0.01174 | no |
| unbounded_output | 100% | 0.01185 | no |

Dominated variants:

- `unbounded_output` is dominated by `bounded_output_artifact`: +0% success at +0.0007015 Cost (USD).
- `bounded_output` is dominated by `bounded_output_artifact`: +0% success at +0.0005954 Cost (USD).

### Success rate vs Steps

| Variant | Success | Steps | Undominated |
| --- | --- | --- | --- |
| bounded_output | 100% | 8.667 | yes |
| bounded_output_artifact | 100% | 8.667 | yes |
| unbounded_output | 100% | 9.111 | no |

Dominated variants:

- `unbounded_output` is dominated by `bounded_output`: +0% success at +0.4444 Steps.


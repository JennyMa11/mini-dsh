# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Steps | Tokens | Cost (USD) | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| no_retry | full | off | detailed | 100% | 10.00 | 9534.0 | 0.095022 | 0% | 1 | 4 |
| single_retry | full | off | detailed | 100% | 10.00 | 9534.0 | 0.095022 | 0% | 1 | 4 |
| basic_feedback | full | off | basic | 100% | 10.00 | 8622.7 | 0.086820 | 0% | 1 | 4 |
| detailed_feedback | full | off | detailed | 100% | 10.00 | 9534.0 | 0.095022 | 0% | 1 | 4 |

# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Steps | Tokens | Cost (USD) | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| unbounded_output | full | off | detailed | 100% | 10.00 | 32136.0 | 0.298440 | 0% | 1 | 4 |
| bounded_output | full | off | detailed | 100% | 10.00 | 9511.3 | 0.094818 | 0% | 1 | 4 |
| bounded_output_artifact | full | off | detailed | 100% | 10.00 | 9534.0 | 0.095022 | 0% | 1 | 4 |

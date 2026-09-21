# Ablation report: mini-dsh-stress

| Variant | Context | Reflection | Feedback | Success | Steps | Tokens | Cost (USD) | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full_history | full | off | detailed | 100% | 10.00 | 9534.0 | 0.095022 | 0% | 1 | 4 |
| sliding_window | sliding | off | detailed | 100% | 10.00 | 8981.0 | 0.090045 | 0% | 1 | 4 |
| summary_compaction | summary | off | detailed | 100% | 10.00 | 9233.7 | 0.092319 | 0% | 1 | 4 |
| repo_retrieval | retrieval | off | detailed | 100% | 10.00 | 12617.0 | 0.122769 | 0% | 1 | 4 |
| retrieval_summary | retrieval_summary | off | detailed | 100% | 10.00 | 12317.0 | 0.120069 | 0% | 1 | 4 |
| symbol_retrieval | symbol | off | detailed | 100% | 10.00 | 9233.7 | 0.092319 | 0% | 1 | 4 |

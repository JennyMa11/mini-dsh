# Ablation report: mini-dsh-core

| Variant | Context | Reflection | Feedback | Success | Steps | Tokens | Cost (USD) | Retry OK | Denied | Blocked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full_history | full | off | detailed | 100% | 4.00 | 749.9 | 0.064545 | 0% | 0 | 0 |
| sliding_window | sliding | off | detailed | 100% | 4.00 | 749.9 | 0.064545 | 0% | 0 | 0 |
| summary_compaction | summary | off | detailed | 100% | 4.00 | 749.9 | 0.064545 | 0% | 0 | 0 |
| repo_retrieval | retrieval | off | detailed | 100% | 4.00 | 1355.1 | 0.109938 | 0% | 0 | 0 |
| retrieval_summary | retrieval_summary | off | detailed | 100% | 4.00 | 1355.1 | 0.109938 | 0% | 0 | 0 |
| symbol_retrieval | symbol | off | detailed | 100% | 4.00 | 1046.1 | 0.086760 | 0% | 0 | 0 |

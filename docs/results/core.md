# Benchmark report: mini-dsh-core

- Schema version: `2.0`
- Tasks: 25
- Success rate: 100%
- Average steps: 4.00
- Average tokens: 749.9
- Average tool calls: 3.32
- Average latency: 23.5 ms
- Total cost: $0.064545
- Retry success rate: 0%
- Permission denied: 0
- Unsafe actions blocked: 0

| Task | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bug-fix-arithmetic | bug-fix | yes | 4 | 631 | 3 | 26.2 | 0.002229 | - |
| bug-fix-comparison | bug-fix | yes | 4 | 690 | 3 | 25.1 | 0.002406 | - |
| bug-fix-string | bug-fix | yes | 4 | 622 | 3 | 25.9 | 0.002202 | - |
| bug-fix-off-by-one | bug-fix | yes | 4 | 671 | 3 | 25.8 | 0.002349 | - |
| bug-fix-parity | bug-fix | yes | 4 | 649 | 3 | 25.5 | 0.002283 | - |
| feature-slugify | feature | yes | 4 | 653 | 3 | 25.8 | 0.002295 | - |
| feature-unique | feature | yes | 4 | 712 | 3 | 25.3 | 0.002472 | - |
| feature-mean | feature | yes | 4 | 649 | 3 | 27.1 | 0.002283 | - |
| feature-word-count | feature | yes | 4 | 651 | 3 | 25.2 | 0.002289 | - |
| test-repair-double | test-repair | yes | 4 | 709 | 3 | 26.7 | 0.002463 | - |
| test-repair-average | test-repair | yes | 4 | 736 | 3 | 25.8 | 0.002544 | - |
| test-repair-area | test-repair | yes | 4 | 723 | 3 | 25.2 | 0.002505 | - |
| test-repair-celsius | test-repair | yes | 4 | 756 | 3 | 25.4 | 0.002604 | - |
| refactor-report | refactor | yes | 4 | 690 | 3 | 18.0 | 0.002406 | - |
| refactor-inventory | refactor | yes | 4 | 698 | 3 | 17.8 | 0.002430 | - |
| refactor-scores | refactor | yes | 4 | 715 | 3 | 18.2 | 0.002481 | - |
| refactor-ledger | refactor | yes | 4 | 699 | 3 | 18.1 | 0.002433 | - |
| understanding-retry | code-understanding | yes | 4 | 648 | 3 | 18.1 | 0.002256 | - |
| understanding-defaults | code-understanding | yes | 4 | 659 | 3 | 17.6 | 0.002289 | - |
| understanding-rate-limits | code-understanding | yes | 4 | 606 | 3 | 17.3 | 0.002130 | - |
| understanding-timeouts | code-understanding | yes | 4 | 611 | 3 | 19.3 | 0.002145 | - |
| multi-file-discount | multi-file | yes | 4 | 1153 | 5 | 26.2 | 0.003795 | - |
| multi-file-tax | multi-file | yes | 4 | 1137 | 5 | 26.6 | 0.003747 | - |
| multi-file-fees | multi-file | yes | 4 | 1124 | 5 | 28.0 | 0.003708 | - |
| multi-file-cart | multi-file | yes | 4 | 1155 | 5 | 26.9 | 0.003801 | - |

## Failure types

- `none`: 25

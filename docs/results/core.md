# Benchmark report: mini-dsh-core

- Schema version: `2.0`
- Tasks: 25 (25 runs)
- Success rate: 100%
- Pass@k (task solved at least once): 100%
- Average steps: 4.00
- Average tokens: 749.9
- Cached input tokens: 0 (0% of input)
- Average tool calls: 3.32
- Average latency: 141.0 ms (model 0.1 / tool 63.1 / harness 77.8)
- Total cost: $0.064545
- Cost per successful run: $0.002582
- Retry success rate: 0%
- Permission denied: 0
- Unsafe actions blocked: 0

## Outcome by layer

| Layer | Runs | Meaning |
| --- | --- | --- |
| success | 25 | finished and the criterion passed |
| harness | 0 | the runtime correctly bounded the agent |
| agent | 0 | the model had a working harness and still failed |
| environment | 0 | the model provider failed |

| Task | Run | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Category of failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bug-fix-arithmetic | 0 | bug-fix | yes | 4 | 631 | 3 | 125.3 | 0.002229 | none |
| bug-fix-comparison | 0 | bug-fix | yes | 4 | 690 | 3 | 133.5 | 0.002406 | none |
| bug-fix-string | 0 | bug-fix | yes | 4 | 622 | 3 | 126.0 | 0.002202 | none |
| bug-fix-off-by-one | 0 | bug-fix | yes | 4 | 671 | 3 | 138.6 | 0.002349 | none |
| bug-fix-parity | 0 | bug-fix | yes | 4 | 649 | 3 | 151.4 | 0.002283 | none |
| feature-slugify | 0 | feature | yes | 4 | 653 | 3 | 144.2 | 0.002295 | none |
| feature-unique | 0 | feature | yes | 4 | 712 | 3 | 163.0 | 0.002472 | none |
| feature-mean | 0 | feature | yes | 4 | 649 | 3 | 183.0 | 0.002283 | none |
| feature-word-count | 0 | feature | yes | 4 | 651 | 3 | 170.6 | 0.002289 | none |
| test-repair-double | 0 | test-repair | yes | 4 | 709 | 3 | 140.3 | 0.002463 | none |
| test-repair-average | 0 | test-repair | yes | 4 | 736 | 3 | 140.6 | 0.002544 | none |
| test-repair-area | 0 | test-repair | yes | 4 | 723 | 3 | 133.1 | 0.002505 | none |
| test-repair-celsius | 0 | test-repair | yes | 4 | 756 | 3 | 175.9 | 0.002604 | none |
| refactor-report | 0 | refactor | yes | 4 | 690 | 3 | 120.2 | 0.002406 | none |
| refactor-inventory | 0 | refactor | yes | 4 | 698 | 3 | 121.8 | 0.002430 | none |
| refactor-scores | 0 | refactor | yes | 4 | 715 | 3 | 118.6 | 0.002481 | none |
| refactor-ledger | 0 | refactor | yes | 4 | 699 | 3 | 120.0 | 0.002433 | none |
| understanding-retry | 0 | code-understanding | yes | 4 | 648 | 3 | 116.1 | 0.002256 | none |
| understanding-defaults | 0 | code-understanding | yes | 4 | 659 | 3 | 117.5 | 0.002289 | none |
| understanding-rate-limits | 0 | code-understanding | yes | 4 | 606 | 3 | 117.3 | 0.002130 | none |
| understanding-timeouts | 0 | code-understanding | yes | 4 | 611 | 3 | 116.1 | 0.002145 | none |
| multi-file-discount | 0 | multi-file | yes | 4 | 1153 | 5 | 163.1 | 0.003795 | none |
| multi-file-tax | 0 | multi-file | yes | 4 | 1137 | 5 | 162.0 | 0.003747 | none |
| multi-file-fees | 0 | multi-file | yes | 4 | 1124 | 5 | 164.3 | 0.003708 | none |
| multi-file-cart | 0 | multi-file | yes | 4 | 1155 | 5 | 161.8 | 0.003801 | none |

## Failure categories

- `none`: 25

## Raw failure types

- `none`: 25

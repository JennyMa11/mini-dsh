# Benchmark report: mini-dsh-stress

- Schema version: `2.0`
- Tasks: 3
- Success rate: 100%
- Average steps: 10.00
- Average tokens: 9534.0
- Average tool calls: 9.00
- Average latency: 45.3 ms
- Total cost: $0.095022
- Retry success rate: 0%
- Permission denied: 1
- Unsafe actions blocked: 4

| Task | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| stress-long-history | long-history | yes | 18 | 10253 | 17 | 79.5 | 0.036135 | - |
| stress-large-output | large-output | yes | 4 | 16059 | 3 | 28.5 | 0.049521 | - |
| stress-denied-action | permission | yes | 8 | 2290 | 7 | 27.8 | 0.009366 | - |

## Failure types

- `none`: 3

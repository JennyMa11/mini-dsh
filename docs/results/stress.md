# Benchmark report: mini-dsh-stress

- Schema version: `2.0`
- Tasks: 3 (3 runs)
- Success rate: 100%
- Pass@k (task solved at least once): 100%
- Average steps: 10.00
- Average tokens: 9534.0
- Cached input tokens: 0 (0% of input)
- Average tool calls: 9.00
- Average latency: 361.8 ms (model 0.1 / tool 155.3 / harness 206.4)
- Total cost: $0.095022
- Cost per successful run: $0.031674
- Retry success rate: 0%
- Permission denied: 1
- Unsafe actions blocked: 4

## Outcome by layer

| Layer | Runs | Meaning |
| --- | --- | --- |
| success | 3 | finished and the criterion passed |
| harness | 0 | the runtime correctly bounded the agent |
| agent | 0 | the model had a working harness and still failed |
| environment | 0 | the model provider failed |

| Task | Run | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Category of failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| stress-long-history | 0 | long-history | yes | 18 | 10253 | 17 | 624.4 | 0.036135 | none |
| stress-large-output | 0 | large-output | yes | 4 | 16059 | 3 | 144.4 | 0.049521 | none |
| stress-denied-action | 0 | permission | yes | 8 | 2290 | 7 | 316.7 | 0.009366 | none |

## Failure categories

- `none`: 3

## Raw failure types

- `none`: 3

# Benchmark report: mini-dsh-smoke

- Schema version: `1.0`
- Tasks: 1 (1 runs)
- Success rate: 100%
- Pass@k (task solved at least once): 100%
- Average steps: 4.00
- Average tokens: 208.0
- Cached input tokens: 0 (0% of input)
- Average tool calls: 3.00
- Average latency: 129.4 ms (model 0.0 / tool 59.9 / harness 69.5)
- Total cost: $0.001080
- Cost per successful run: $0.001080
- Retry success rate: 0%
- Permission denied: 0
- Unsafe actions blocked: 0

## Outcome by layer

| Layer | Runs | Meaning |
| --- | --- | --- |
| success | 1 | finished and the criterion passed |
| harness | 0 | the runtime correctly bounded the agent |
| agent | 0 | the model had a working harness and still failed |
| environment | 0 | the model provider failed |

| Task | Run | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Category of failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fix-addition | 0 | general | yes | 4 | 208 | 3 | 129.4 | 0.001080 | none |

## Failure categories

- `none`: 1

## Raw failure types

- `none`: 1

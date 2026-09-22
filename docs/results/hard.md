# Benchmark report: mini-dsh-hard

- Schema version: `2.1`
- Tasks: 4 (20 runs)
- Success rate: 95%
- Pass@k (task solved at least once): 100%
- Average steps: 10.10
- Average tokens: 50049.9
- Cached input tokens: 868864 (93% of input)
- Average tool calls: 12.85
- Average latency: 17099.2 ms (model 16911.1 / tool 156.2 / harness 32.0)
- Total cost: $0.051163
- Cost per successful run: $0.002693
- Retry success rate: 0%
- Permission denied: 0
- Unsafe actions blocked: 34

## Model and rate card

- `label`: "deepseek-flash"
- `pricing.cached_input_usd_per_million`: 0.003
- `pricing.input_usd_per_million`: 0.15
- `pricing.output_usd_per_million`: 0.6
- `profile.context_window`: 1000000
- `profile.endpoint`: "https://api.deepseek.com/chat/completions"
- `profile.model`: "deepseek-flash"
- `profile.pricing_as_of`: ""
- `profile.pricing_declared`: false
- `profile.pricing_source`: "DeepSeek publishes USD rates on a peak/off-peak schedule: off-peak is half of peak, and peak hours are 01:00-04:00 and 06:00-10:00 UTC Monday-Friday, excluding Chinese public holidays. A single rate card therefore cannot describe a run that crosses a tariff boundary, so the rates are deliberately not pinned here. As of 2026-09-21 the official page lists deepseek-flash at $0.003 cache-hit input / $0.15 cache-miss input / $0.6 output per 1M tokens off-peak (double at peak), and deepseek-v4-pro at $0.022 / $0.66 / $1.98. Read https://api-docs.deepseek.com/quick_start/pricing and pass the rates explicitly (--input-usd-per-million / --output-usd-per-million / --cached-input-usd-per-million) before quoting any cost number."
- `profile.profile`: "deepseek"
- `profile.temperature_effective`: true
- `profile.thinking`: false

## Outcome by layer

| Layer | Runs | Meaning |
| --- | --- | --- |
| success | 19 | finished and the criterion passed |
| harness | 1 | the runtime correctly bounded the agent |
| agent | 0 | the model had a working harness and still failed |
| environment | 0 | the model provider failed |

- Repeated tasks: 4; mean success spread 0.25; mean token spread 64%

| Task | Run | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Category of failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hard-interval-merge | 0 | hard-spec | yes | 5 | 16415 | 7 | 10037.2 | 0.001500 | none |
| hard-interval-merge | 1 | hard-spec | yes | 8 | 35295 | 11 | 14632.3 | 0.002285 | none |
| hard-interval-merge | 2 | hard-spec | no | 17 | 151854 | 21 | 37088.7 | 0.006316 | budget_exhausted |
| hard-interval-merge | 3 | hard-spec | yes | 5 | 14483 | 7 | 7175.3 | 0.001016 | none |
| hard-interval-merge | 4 | hard-spec | yes | 11 | 66041 | 15 | 23762.8 | 0.003692 | none |
| hard-token-bucket | 0 | hard-spec | yes | 12 | 54565 | 15 | 18943.3 | 0.002533 | none |
| hard-token-bucket | 1 | hard-spec | yes | 8 | 30047 | 10 | 12854.0 | 0.001868 | none |
| hard-token-bucket | 2 | hard-spec | yes | 18 | 102958 | 22 | 27449.4 | 0.003816 | none |
| hard-token-bucket | 3 | hard-spec | yes | 14 | 74025 | 18 | 22382.7 | 0.003548 | none |
| hard-token-bucket | 4 | hard-spec | yes | 18 | 102500 | 22 | 25750.1 | 0.003662 | none |
| hard-config-precedence | 0 | hard-spec | yes | 7 | 21068 | 8 | 9903.1 | 0.001420 | none |
| hard-config-precedence | 1 | hard-spec | yes | 8 | 23801 | 9 | 10679.6 | 0.001563 | none |
| hard-config-precedence | 2 | hard-spec | yes | 8 | 24361 | 9 | 11570.6 | 0.001684 | none |
| hard-config-precedence | 3 | hard-spec | yes | 8 | 23881 | 9 | 11194.5 | 0.001466 | none |
| hard-config-precedence | 4 | hard-spec | yes | 8 | 24328 | 9 | 11754.3 | 0.001759 | none |
| hard-csv-records | 0 | hard-spec | yes | 9 | 39640 | 13 | 16471.7 | 0.002623 | none |
| hard-csv-records | 1 | hard-spec | yes | 6 | 18356 | 9 | 10670.6 | 0.001393 | none |
| hard-csv-records | 2 | hard-spec | yes | 13 | 65395 | 17 | 20185.7 | 0.002792 | none |
| hard-csv-records | 3 | hard-spec | yes | 13 | 92772 | 17 | 27918.1 | 0.004660 | none |
| hard-csv-records | 4 | hard-spec | yes | 6 | 19214 | 9 | 11559.9 | 0.001566 | none |

## Failure categories

- `budget_exhausted`: 1
- `none`: 19

## Raw failure types

- `budget_exhausted`: 1
- `none`: 19

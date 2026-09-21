# Benchmark report: mini-dsh-repo

- Schema version: `2.0`
- Tasks: 6 (18 runs)
- Success rate: 100%
- Pass@k (task solved at least once): 100%
- Average steps: 6.61
- Average tokens: 19132.2
- Cached input tokens: 284032 (86% of input)
- Average tool calls: 10.28
- Average latency: 6410.1 ms (model 6290.6 / tool 103.1 / harness 16.4)
- Total cost: $0.016865
- Cost per successful run: $0.000937
- Retry success rate: 0%
- Permission denied: 0
- Unsafe actions blocked: 20

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
| success | 18 | finished and the criterion passed |
| harness | 0 | the runtime correctly bounded the agent |
| agent | 0 | the model had a working harness and still failed |
| environment | 0 | the model provider failed |

- Repeated tasks: 6; mean success spread 0.00; mean token spread 15%

| Task | Run | Category | Success | Steps | Tokens | Tool calls | Latency (ms) | Cost (USD) | Category of failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| repo-cross-file-bug | 0 | repo-bug-fix | yes | 7 | 18217 | 9 | 5889.6 | 0.000861 | none |
| repo-cross-file-bug | 1 | repo-bug-fix | yes | 7 | 18103 | 9 | 6354.1 | 0.000798 | none |
| repo-cross-file-bug | 2 | repo-bug-fix | yes | 6 | 15948 | 9 | 4835.8 | 0.000736 | none |
| repo-callsite-contract | 0 | repo-callsite | yes | 6 | 22483 | 12 | 5938.6 | 0.001174 | none |
| repo-callsite-contract | 1 | repo-callsite | yes | 6 | 21187 | 12 | 6451.2 | 0.001141 | none |
| repo-callsite-contract | 2 | repo-callsite | yes | 6 | 22838 | 12 | 5982.2 | 0.001211 | none |
| repo-failing-test-localization | 0 | repo-test-repair | yes | 7 | 17621 | 8 | 6049.1 | 0.000783 | none |
| repo-failing-test-localization | 1 | repo-test-repair | yes | 7 | 19079 | 8 | 6650.1 | 0.000799 | none |
| repo-failing-test-localization | 2 | repo-test-repair | yes | 7 | 17474 | 8 | 5546.3 | 0.000733 | none |
| repo-feature-with-tests | 0 | repo-feature | yes | 6 | 16936 | 10 | 6994.5 | 0.001012 | none |
| repo-feature-with-tests | 1 | repo-feature | yes | 7 | 21203 | 12 | 7439.6 | 0.001158 | none |
| repo-feature-with-tests | 2 | repo-feature | yes | 7 | 21780 | 12 | 8182.3 | 0.001308 | none |
| repo-config-change | 0 | repo-config | yes | 6 | 16433 | 9 | 5642.5 | 0.000739 | none |
| repo-config-change | 1 | repo-config | yes | 6 | 16443 | 9 | 5935.0 | 0.000632 | none |
| repo-config-change | 2 | repo-config | yes | 6 | 16442 | 9 | 5299.0 | 0.000632 | none |
| repo-error-recovery | 0 | repo-error-recovery | yes | 6 | 16435 | 11 | 6381.7 | 0.000956 | none |
| repo-error-recovery | 1 | repo-error-recovery | yes | 9 | 26318 | 14 | 8554.3 | 0.001168 | none |
| repo-error-recovery | 2 | repo-error-recovery | yes | 7 | 19439 | 12 | 7256.5 | 0.001023 | none |

## Failure categories

- `none`: 18

## Raw failure types

- `none`: 18

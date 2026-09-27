# hard 真实模型 Context 对照

> 生成来源：`uv run python scripts/archive_hard_context.py --source benchmark-results/hard-context-20260927 --output docs/results/hard-real-context-ablation-2026-09-27.md`；输入为同目录 metadata.json、ablation.json、audit-summary.json 和原始 Trace。

## 实验条件

- 实现提交：`8cb18cb3ae7c96518fbb5889980f727b8bc904fd`；启动时工作区有改动：`False`。
- 源码与输入摘要：`d0baade3554dfd3b8d3fc004ae8bf7dee9e3f8a6848c58dd978cb8d63945c4cf`。
- 开始：`2026-09-27T11:48:51.375229+00:00`；结束：`2026-09-27T11:55:18.355684+00:00`。
- 模型：`deepseek-flash`，temperature=0，thinking 关闭；每任务每策略 3 次，按轮次和任务交替策略顺序。
- 唯一改变的 Runtime 选项为 context_strategy；固定 hard 的四个任务、留出判据、预算、工具、JSONL Journal、20,000 字符输出上限、详细反馈与 artifact；reflection 关闭。
- Summary 保留最近 12 条消息，摘要上限 4,000 字符，不增加模型摘要调用。
- 本地 runner 只用于可信 fixture；不提供操作系统级隔离。
- 声明费率（每百万 Token）：`{"cached_input_usd_per_million": 0.003, "input_usd_per_million": 0.15, "output_usd_per_million": 0.6}`。
- 费率核对日期：`2026-09-27`；时段：`off-peak (Sunday)`。来源：[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)。

## 样本结果

| 策略 | 判据通过 | 平均记录 Token | 累计记录 USD | 记录 USD/成功 | 缓存输入份额 | 平均秒 |
| --- | --- | --- | --- | --- | --- | --- |
| full_history | 11/12 | 44,017.50 | $0.02966643 | $0.00269695 | 90.65% | 15.84 |
| summary_compaction | 12/12 | 40,643.17 | $0.04867525 | $0.00405627 | 62.35% | 16.27 |

累计记录计价 $0.07834168。这不是账户账单；下文说明一个失败调用的用量缺失。
摘要相对 Full History 的平均记录 Token 变化为 -7.67%，记录费用变化为 +64.08%。

## 逐任务观察

| 任务 | Full 通过 | Summary 通过 | Full 三轮 Token | Summary 三轮 Token | 摘要生效轮数 |
| --- | --- | --- | --- | --- | --- |
| hard-interval-merge | 3/3 | 3/3 | 17198, 45697, 109704 | 15008, 110177, 78885 | 2/3 |
| hard-token-bucket | 2/3 | 3/3 | 61407, 13093, 84875 | 34470, 48603, 66163 | 3/3 |
| hard-config-precedence | 3/3 | 3/3 | 46835, 23067, 23980 | 23828, 23644, 23877 | 3/3 |
| hard-csv-records | 3/3 | 3/3 | 35667, 18624, 48063 | 25563, 18636, 18864 | 1/3 |

## 失败、计价缺口与解释

- `full_history` / `hard-token-bucket` / trial=1：`ModelAdapterError[invalid_tool_arguments]: model returned invalid tool arguments for shell`；分类 `model_reasoning_failure`，层 `agent`，验证器退出码 `None`。
- 1 个模型调用仅有 started，未产生 completed 用量事件。未成为 ModelResponse 的失败调用没有可用的 usage 计数；有缺失时，计价与 Token 总量不是完整账单口径。该缺口应单独修复，不在实验运行中更改实现或补跑失败样本。
- 本轮每策略 12 次样本；少量参数解析失败不能证明摘要提高了稳定成功率。
- 摘要在 9/12 个样本中实际触发；未触发的样本不能用来证明压缩本身的效果。
- 记录到的缓存输入份额分别为 90.65%、62.35%。Token 减少不等于费用同比下降；共享 provider 缓存与采样波动仍是限制。
- Trace 一致性审计只说明回放与报告一致，不证明每个失败调用的用量都被完整记录。

## 审计与复现

- 24 份 Trace 的终态、Token 和计价与报告一致；23/24 通过判据；实际凭据匹配数为 0。
- 原始 JSON/JSONL、Trace 与单次任务报告保留在实验目录，被 Git 忽略。
- 完整固定条件与执行入口见[两策略实验协议](../hard-context-experiment.md)。检出上述实现提交，使用锁文件安装并设置凭据，重新核对适用费率后运行；真实模型结果允许波动。
- 原始数据仍在本机时，可用本文件首行的归档脚本重新生成本报告。

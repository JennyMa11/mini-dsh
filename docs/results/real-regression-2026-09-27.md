# 固定版本的 DeepSeek 小规模回归（2026-09-27）

> 数据来源：`mini-dsh benchmark` 生成的 repo.json / hard.json；原始报告、Trace、metadata.json、audit-summary.json 与本地 driver.py 位于 `benchmark-results/real-regression-20260927/`，未提交。表格从这些实际报告汇总，复现命令见文末。

## 运行条件

- 实现提交：`2e7d35158e4d70fbc63c4c75e9cf6e9f39f43975`；启动时工作区干净，结束时源码摘要未变化。
- 源码与输入摘要：`e39980d1bbd172c2f5f6f2e0659e5a97d3a18e7d0a1f845c81925f558652992e`。
- 开始：`2026-09-27T11:34:44.828361+00:00`；结束：`2026-09-27T11:36:40.503805+00:00`。
- 模型：`deepseek-flash`；官方文档当日对应 DeepSeek-V4.1-Flash。此版本说明来自官方文档，并非 API 返回的不可变模型快照。
- 单 Agent、full_history、temperature=0、thinking 关闭、reflection 关闭；每任务采样一次。
- 保留原 manifest 的 fixture、预算、隔离与成功判据：repo 6 任务，hard 4 任务；hard 的留出判据在 Agent 结束之后落地。
- 工具输出上限 20,000 字符，artifact 开启，详细反馈开启，工具重试上限 1；模型瞬时错误最多尝试 3 次。
- 使用可信 fixture 的 local runner；工具网络默认关闭，仅显式授予写入、Shell 和 python 命令。local runner 不提供操作系统级隔离。
- 实验进程采用环境 allowlist，仅为模型连接注入凭据；凭据通过终端关闭回显的 stdin 进入本地驱动内存，未写入配置或 argv。
- 官方周末 off-peak 费率：输入 $0.15、输出 $0.60、缓存输入 $0.003 / 1M tokens；来源：[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)，2026-09-27 核对。
- 下文费用依据 provider 返回的用量与声明费率计算，不是账户账单核对结果。

## 套件结果

| 套件 | 判据通过 | Token 总量 | 累计 USD | 平均秒/任务 | 平均 Harness ms |
| --- | --- | --- | --- | --- | --- |
| repo | 6/6 | 115,930 | $0.00590615 | 7.13 | 43.60 |
| hard | 4/4 | 210,831 | $0.01123032 | 17.96 | 77.44 |

共 10 个任务通过判据，累计 326,761 Token，计价 $0.01713647。

## 逐任务结果

| 任务 | 判据通过 | 步数 | 工具调用 | Token | USD |
| --- | --- | --- | --- | --- | --- |
| repo-cross-file-bug | 是 | 7 | 9 | 17,786 | $0.00073778 |
| repo-callsite-contract | 是 | 6 | 12 | 22,459 | $0.00123746 |
| repo-failing-test-localization | 是 | 7 | 8 | 17,292 | $0.00069501 |
| repo-feature-with-tests | 是 | 8 | 13 | 25,766 | $0.00142691 |
| repo-config-change | 是 | 6 | 9 | 16,367 | $0.00085920 |
| repo-error-recovery | 是 | 6 | 11 | 16,260 | $0.00094980 |
| hard-interval-merge | 是 | 7 | 9 | 28,752 | $0.00210239 |
| hard-token-bucket | 是 | 15 | 19 | 81,226 | $0.00373666 |
| hard-config-precedence | 是 | 8 | 8 | 23,268 | $0.00161095 |
| hard-csv-records | 是 | 13 | 17 | 77,585 | $0.00378033 |

## 审计与解释

- 10 份 Trace 回放的终态、输入/输出/缓存 Token 和计价与逐任务报告一致；10 个验证器退出码均为 0。
- 运行产物中的 API Key 形态扫描未发现匹配；该检查验证本次产物，不构成通用密钥检测保证。
- 全部终态为 finished，失败层归类为 success；发生 14 次 unsafe_blocked，Agent 仍完成任务。这些策略拒绝不等于最终任务失败。
- repo 使用可见测试，只证明可见判据通过；hard 的成功由留出判据决定。
- 每任务仅一次样本，只用于当前版本的接入与执行回归。不能据此证明真实成功率为 100%，也不能证明优于旧 hard 报告的 19/20。
- 历史报告保留原样；当前 provider 模型别名可能随服务端升级变化。跨时间结果不能把差异全部归因到 Harness。

## 复现

检出上述实现提交，使用 `uv sync --locked --all-groups` 安装。提供 `DEEPSEEK_API_KEY` 环境变量，清除其他 MINIDSH_* 配置和代理环境变量，创建独立输出目录及空配置文件。不得把真实凭据放进命令参数或提交文件。

以下为本轮固定条件；重新运行时先核对当时适用费率，真实模型采样、缓存命中与墙钟结果允许变化：

```bash
mkdir -p benchmark-results/real-regression-new
: > benchmark-results/real-regression-new/empty.toml
for suite in repo hard; do
  uv run mini-dsh benchmark "benchmarks/$suite/manifest.json" \
    --config benchmark-results/real-regression-new/empty.toml \
    --provider openai-compatible --model-profile deepseek --model deepseek-flash \
    --api-key-env DEEPSEEK_API_KEY --temperature 0 --no-thinking --repeat 1 \
    --max-attempts 3 --context full --no-multi-agent --no-reflection-enabled \
    --detailed-tool-feedback --artifact-offload --tool-output-limit 20000 \
    --tool-max-retries 1 --sandbox-backend local --builtin-tools \
    --allow-write --allow-shell --allow-command python --verify-command python \
    --input-usd-per-million 0.15 --output-usd-per-million 0.60 \
    --cached-input-usd-per-million 0.003 \
    --output "benchmark-results/real-regression-new/$suite.json" \
    --markdown "benchmark-results/real-regression-new/$suite.md"
done
```

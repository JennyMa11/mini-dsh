# hard 两策略 Context 对照

`scripts/hard_context_experiment.py` 比较 Full History 与 Summary Compaction。
使用同一 hard manifest、留出判据、预算、模型与工具配置；每个任务每种策略重复三次，
共 24 次执行。唯一更改的 Runtime 选项是 `context_strategy`。

## 调度与固定条件

按照试验轮次、任务、策略依次运行。每个任务的一对策略相邻执行，策略先后顺序按
轮次和任务序号交替；三轮四任务中，每种策略各有六次先执行。这样减少整批先跑一种
策略引入的时间和缓存偏差，但不能消除共享 provider 缓存或远端服务变化。

模型为 deepseek-flash，temperature=0，thinking 关闭，瞬时失败最多尝试三次。
Journal 为 JSONL，工具输出上限 20,000 字符，artifact 与详细反馈开启，reflection
关闭，工具重试上限 1。Summary 保留最近 12 条消息，摘要上限 4,000 字符，仍由既有
Context 策略维护 Tool Call 轮次完整性。策略压缩是确定性的，不增加摘要模型调用。

本地执行只用于可信 fixture；工具网络默认关闭，允许写入与 python 命令。
Local runner 的权限检查不提供操作系统级隔离。

## 执行

先提交实现并通过仓库质量门禁，再设置环境变量 `DEEPSEEK_API_KEY`。凭据只从进程
环境读取，不接受命令行 Key，不自动读取 `.env`，不写入配置或报告。
清除无关 MINIDSH_* 与代理环境变量；按当时适用费率声明计价。

```bash
uv sync --locked --all-groups
uv run python scripts/hard_context_experiment.py \
  --output benchmark-results/hard-context-new \
  --repeat 3 \
  --input-usd-per-million 0.15 \
  --output-usd-per-million 0.60 \
  --cached-input-usd-per-million 0.003
```

示例费率来自 2026-09-27 核对的 [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)，
对应周末 off-peak；未来运行需重新核对。费率是计算口径，不是账户账单验证。

输出只能位于 `benchmark-results/` 下的新目录，已存在目录拒绝覆盖。metadata.json
记录实际提交号、干净状态、源码/输入摘要、全部调度顺序、采样参数与费率；各次执行
完成立即保存单任务报告与原始 Trace。发生 provider_error 时停止实验并保留部分
结果；任务自身失败仍是实验样本，不能删除或补跑成成功。

全部执行结束后生成分策略报告、ablation.json / ablation.md，回放所有 Trace 校验
终态、Token 与费用，并用本次实际凭据检查 JSON、JSONL、Markdown 和日志中的泄露。
审计失败会非零退出；失败样本不等于审计失败。源代码在运行中变化也会拒绝完成。

## 解释

报告成功率、失败类别、步骤、Token、缓存命中、工具调用、延迟与每次成功成本。
每策略仅 12 次执行；若都通过，只能比较这些样本的成本和用量，不能证明两策略准确率
相同。即便出现差异，也需扩大任务集与样本后再讨论稳定的成功率差异。
成本可能受缓存命中左右，Token 减少不一定带来相同比例的费用下降。

核心 Loop 与 BenchmarkRunner 均未改动；该入口只负责有序安排独立单 Agent 运行。

# 离线可复现基线

> 归档来源：`benchmark-results/offline-20260927T112922598243Z/report.md`。下文“同目录”指该原始实验目录；原始 JSON 与 Trace 保留在本机，未提交。
> 使用下文 Git HEAD 检出实现并按复现命令重跑，可重新生成报告；墙钟测量允许波动。

> 生成来源：`scripts/offline_regression.py`；输入为同目录 JSON 报告与 Trace。

- Git HEAD：`c64eade983ac47eb35803d062657b3462abf5357`；工作区有改动：`False`。
- 源码、测试、实验脚本与输入摘要：`e39980d1bbd172c2f5f6f2e0659e5a97d3a18e7d0a1f845c81925f558652992e`。
- 模型：ScriptedModel；无真实模型调用，实际 API 费用 $0。
- 成本为输入 $3 / 输出 $15 per 1M tokens 的模拟值，不代表账单。
- 显式空配置、单 Agent、本地后端、默认工具断网；凭据及 profile 环境变量不传入。
- 墙钟耗时允许波动；脚本结果不能证明真实模型任务能力。

| 实验 | 变体 | 执行次数 | 判据通过 | 平均估算 Token |
| --- | --- | --- | --- | --- |
| smoke | baseline | 2 | 100% | 208.00 |
| core | baseline | 50 | 100% | 749.88 |
| stress | baseline | 6 | 100% | 9,534.00 |
| core-context | full_history | 50 | 100% | 749.88 |
| core-context | sliding_window | 50 | 100% | 749.88 |
| core-context | summary_compaction | 50 | 100% | 749.88 |
| core-context | repo_retrieval | 50 | 100% | 1,355.12 |
| core-context | retrieval_summary | 50 | 100% | 1,355.12 |
| core-context | symbol_retrieval | 50 | 100% | 1,046.08 |
| core-tool_output | unbounded_output | 50 | 100% | 749.88 |
| core-tool_output | bounded_output | 50 | 100% | 749.88 |
| core-tool_output | bounded_output_artifact | 50 | 100% | 749.88 |
| core-permission | no_retry | 50 | 100% | 749.88 |
| core-permission | single_retry | 50 | 100% | 749.88 |
| core-permission | basic_feedback | 50 | 100% | 653.00 |
| core-permission | detailed_feedback | 50 | 100% | 749.88 |
| stress-context | full_history | 6 | 100% | 9,534.00 |
| stress-context | sliding_window | 6 | 100% | 8,981.00 |
| stress-context | summary_compaction | 6 | 100% | 9,233.67 |
| stress-context | repo_retrieval | 6 | 100% | 12,617.00 |
| stress-context | retrieval_summary | 6 | 100% | 12,317.00 |
| stress-context | symbol_retrieval | 6 | 100% | 9,233.67 |
| stress-tool_output | unbounded_output | 6 | 100% | 32,136.00 |
| stress-tool_output | bounded_output | 6 | 100% | 9,511.33 |
| stress-tool_output | bounded_output_artifact | 6 | 100% | 9,534.00 |
| stress-permission | no_retry | 6 | 100% | 9,534.00 |
| stress-permission | single_retry | 6 | 100% | 9,534.00 |
| stress-permission | basic_feedback | 6 | 100% | 8,622.67 |
| stress-permission | detailed_feedback | 6 | 100% | 9,534.00 |

共 1010 次判据通过；393 对重复任务确定性指标一致。
Session 对照 112 对，确定性指标一致；Harness 配对耗时增量中位数 0.859 ms。
全部 Trace 回放终态、Token、模拟成本与报告一致；确定性指标与历史归档一致。

复现命令：

```bash
uv sync --locked --all-groups
uv run python scripts/offline_regression.py
```

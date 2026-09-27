# 模型失败调用的用量记录

`Model.complete` 可以抛出领域契约 `ModelCallError`。错误携带安全的消息、`code`、`retryable`，以及可选的 `TokenUsage`。`usage=None` 表示供应商没有提供可验证的用量；`TokenUsage()` 表示供应商明确报告零用量，两者不可混同。现有 `ModelAdapterError` 继承此契约，保留原有错误名称与分类。

OpenAI-compatible adapter 在解析工具参数之前，独立校验响应中的用量。即使 choices 格式错误、choices 为空，或工具参数不是合法 JSON 对象，已知输入、输出和缓存 Token 仍随异常传递。缺少输入或输出计数、负数、错误类型和缓存数量超过输入数量时，不将其视为已知用量。

Agent Loop 对失败调用的已知用量计费并累加至运行计数，发布 `model_call_failed` 事件。事件包含 `usage_known`、用量、计算费用、耗时和失败原因；未知用量字段为 `null`。这不会执行工具，也不会把失败响应加入助手消息。单 Agent 解析失败仍以 `failed` 结束，即使此次用量达到 Token 上限。可选协作包装层也计入团队预算，并保留其现有团队预算终态规则。

Trace 汇总、回放和延迟视图同时读取 `model_call_completed` 与 `model_call_failed`，失败步骤显示为 `failed`。凭据脱敏包装保留用量与错误分类。网络异常、超时和取消没有已知用量时仍明确记录未知；运行总费用仅代表已记录调用的计算费用，不是供应商账单。

本修复适用于后续运行。此前的 [24 次实测报告](results/hard-real-context-ablation-2026-09-27.md) 与原始 Trace 保持不变；其中失败调用的未记录用量无法从现存数据恢复，不进行推算或补写。

回归测试覆盖参数 JSON 无效、参数不是对象、错误响应仍携带有效用量、未知用量、已知零用量、缓存定价、失败状态、凭据脱敏、团队汇总，以及 Trace 回放与延迟重建。所有测试使用本地受控输入，不调用真实模型服务。

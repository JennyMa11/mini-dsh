# ADR 0031：Phoenix / OpenInference 可观测性接入

- 状态：Accepted with limitations
- 日期：2026-11-10

## 背景

Mini-dsh 已把 Model Call、Tool Call、Context 变换、验证和终态发布为结构化 Event，并有
JSONL Trace 作为审计事实源。做 Harness 对照时，仍需要手动解析 Trace 才能回答“哪种配置
成功率更高、Token 更少、哪些工具反复失败、截断是否丢了信息”。

Arize Phoenix 基于 OpenTelemetry / OpenInference，提供 Trace 浏览、按属性筛选以及
Dataset / Experiment 能力。目标是让同一批事件同时进入本地 Phoenix，形成可查询、可比较
的数据面，而不把 Phoenix 变成执行依赖。

## 决策

- 新增 `adapters/phoenix.py`，把 Event Bus 事件翻译为 OpenInference span：
  `agent.run`（AGENT）、`llm.complete`（LLM）、`tool.<name>`（TOOL）、
  `context.<strategy>`（CHAIN）。
- 接入点是 `build_local_runtime` 的事件订阅，不修改 Agent Loop 的模型、工具、权限和预算
  路径。Runtime 关闭时先退订，再 `shutdown` TracerProvider 以刷新未完成 span。
- OpenTelemetry、OTLP HTTP exporter 和 OpenInference 语义约定放在 `phoenix` 可选 extra；
  默认安装不引入 OTel，未启用时也不创建 provider。
- 配置沿用分层规则：`MINIDSH_PHOENIX_*`、`minidsh.toml` 和
  `--phoenix*` CLI 参数，默认关闭。Endpoint 接受 Phoenix 根地址或完整 OTLP traces URL。
- 根 span 记录本次 Harness 配置（`harness.config.*`），便于在 Phoenix 中按
  `tool_output_limit`、`context_strategy` 等变量分组。
- 内容复用 JSONL Trace 的脱敏规则并限制单属性长度；`phoenix_capture_content=false` 时
  只导出结构、Token、耗时和状态。
- 不接受把 `finished` 当作任务成功。根 span 同时记录
  `harness.verification.status` 和 `harness.test.*`，独立判据仍是成功来源。
- 被拒绝或参数无效的工具调用即使没有 `tool_call_started`，也在
  `tool_call_completed` 时补建 span，避免失败动作在轨迹中消失。

## 替代方案

- 直接在 Agent Loop 中调用 OTel SDK：会把可选依赖和厂商语义写入核心路径，破坏端口方向。
- 只依赖 JSONL Trace：可审计，但缺少统一 span 父子关系、筛选和实验对比界面。
- 以 Phoenix 服务可达作为测试前提：本地服务、端口和网络不稳定，不适合作为离线门禁。
- 由模型自报任务成功：违反“可执行判据优先”，且会把 `finished` 与任务正确性混为一谈。

## 后果与边界

- Phoenix 是可选的外部观测面。删除该 extra 或关闭配置后，Runtime 行为与 Trace 不变。
- 适配器只导出 OTLP HTTP，暂不处理 Phoenix Cloud 认证头；需要鉴权时应通过受控代理或
  后续显式 header 配置扩展。
- 批量导出可能有一定延迟；Runtime 关闭时刷新。进程被强制终止时，未刷新的 span 可能丢失，
  但 JSONL Trace 与 Session 事实不受影响。
- 当前测试用真实 OTel SDK + InMemorySpanExporter 验证映射、父子关系、脱敏、截断和关闭
  生命周期，不把本地 Phoenix 服务可达作为自动化前提。

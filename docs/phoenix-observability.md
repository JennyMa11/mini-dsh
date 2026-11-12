# Phoenix 可观测性：把执行轨迹变成可查询数据

Phoenix 接入是可选的 OpenTelemetry / OpenInference 导出层，不改变 Agent Loop 的预算、
权限、工具或 Context 语义。它把 Event Bus 上已有的结构化事实翻译成 span，发送到本地或远程
的 Arize Phoenix，用于查看单次轨迹、按 Harness 配置筛选，以及做前后对照。

## 1. 启动 Phoenix

```bash
uvx arize-phoenix serve
# http://localhost:6006
```

Phoenix 接收 OTLP HTTP。Mini-dsh 默认把 span 发到
`http://localhost:6006/v1/traces`；配置里既可以写 Phoenix 根地址，也可以写完整的
traces URL。

## 2. 启用导出

安装可选依赖并运行：

```bash
uv sync --extra phoenix
uv run --extra phoenix mini-dsh run "修复失败测试" --workspace . --phoenix
```

也可以只用环境变量或 `minidsh.toml`：

```bash
export MINIDSH_PHOENIX_ENABLED=true
export MINIDSH_PHOENIX_ENDPOINT=http://localhost:6006
export MINIDSH_PHOENIX_PROJECT=mini-dsh
```

```toml
[minidsh]
phoenix_enabled = true
phoenix_endpoint = "http://localhost:6006"
phoenix_project = "mini-dsh"
phoenix_capture_content = true
```

优先级仍是 CLI > 环境变量 > 配置文件 > 安全默认值。对应 CLI 参数为
`--phoenix/--no-phoenix`、`--phoenix-endpoint`、`--phoenix-project` 和
`--phoenix-capture-content/--no-phoenix-capture-content`。

未安装可选依赖时，只有显式启用 Phoenix 才会报错并提示安装方式；默认运行不需要
OpenTelemetry。

## 3. Span 结构

一次 Agent Run 形成一棵以 `agent.run` 为根的 trace：

| Span | OpenInference kind | 来源事件 | 主要属性 |
| --- | --- | --- | --- |
| `agent.run` | `AGENT` | `run_started` / `run_finished` | `harness.run.status`、`harness.steps`、Token、缓存输入、成本、时延拆分、失败原因 |
| `llm.complete` | `LLM` | `model_call_started` / `model_call_completed` / `model_call_failed` | `llm.model_name`、prompt/completion/total Token、缓存读取、`llm.finish_reason`、Prompt/Response |
| `tool.<name>` | `TOOL` | `tool_call_started` / `tool_call_completed` | 工具名、参数、Observation、`harness.tool.truncated`、重试次数、退出码 |
| `context.<strategy>` | `CHAIN` | `context_built` | 策略/版本、估算 Token、省略消息数、`context.tokens_saved`、省略原因 |

补充事实：

- 上下文发生截断时，根 span 追加 `context_truncated` 事件；
- 运行时验证追加 `runtime_verification_completed` 事件，根 span 记录
  `harness.verification.status`、`criterion_version` 和 `reason`；
- `test` 工具的退出码写入 `harness.test.exit_code` 和 `harness.test.passed`；
- 每个根 span 记录本次 Harness 配置（`harness.config.*`），例如
  `context_strategy`、`tool_output_limit`、`normalize_tool_results`、
  `reflection_enabled`、`local_first_enabled` 和 `sandbox_backend`。

工具调用即使在校验或权限阶段被拒绝、没有产生 `tool_call_started`，也仍会生成一个
`tool.<name>` span，状态为 `ERROR`，因此拒绝和无效调用不会在轨迹中消失。

## 4. 安全与边界

- 内容在进入 span 前复用与 JSONL Trace 相同的脱敏规则；疑似密钥字段和 Bearer Token
  会被替换为 `[REDACTED]`。
- 单个内容属性最多 50,000 字符，超出部分显式截断。
- `phoenix_capture_content = false` 时不发送 Prompt、Response、工具输出和 Context metadata，
  只保留角色、Token、耗时、状态和计数属性。
- Endpoint 拒绝 URL 内嵌凭据、查询串和 fragment。
- Phoenix 关闭或 Runtime 关闭时，未完成的 span 会以 `ERROR` 结束并刷新导出。批量导出
  由 OpenTelemetry `BatchSpanProcessor` 负责，进程退出前会 `shutdown`。
- 根 span 的 `finished` 只表示 Loop 正常结束，不等于任务成功。任务是否真的完成要看
  `harness.verification.status`、`harness.test.*` 或仓库外的独立判据。

## 5. 比较不同 Harness 配置

Phoenix 的价值在于把“某次成功/失败”变成可分组的数据。建议固定模型、任务集、预算和
工作区，只改一个 Harness 变量：

```bash
# A：默认工具输出上限
uv run --extra phoenix mini-dsh run "修复失败测试" --workspace . --phoenix \
  --phoenix-project harness-tool-output-a --tool-output-limit 20000

# B：更小的工具输出上限
uv run --extra phoenix mini-dsh run "修复失败测试" --workspace . --phoenix \
  --phoenix-project harness-tool-output-b --tool-output-limit 4000
```

在 Phoenix 中按 `harness.config.tool_output_limit`、`harness.config.context_strategy` 等
属性筛选，再比较：

| 问题 | 观察位置 |
| --- | --- |
| 任务成功率是否变化 | `harness.verification.status`、`harness.test.passed`，必要时结合独立验证 sidecar |
| Token 是否下降 | 根 span 的 `harness.tokens`、LLM span 的 `llm.token_count.*` |
| 哪些工具失败或重试 | `tool.*` span 的 `harness.tool.status`、`harness.tool.retries`、`exception.*` |
| 截断是否丢失信息 | `context.omitted_messages`、`context.omitted_estimated_tokens`、`context.tokens_saved`、`harness.tool.truncated` |
| 成本与延迟变化 | `harness.cost_usd`、`harness.latency.*`、每个 LLM/Tool span 的耗时 |

Phoenix 的 Dataset / Experiment 适合把这组对照扩展为多条任务：每条任务仍是同一
Harness 配置的一次真实运行，评分必须来自独立测试或判据，不能把模型自报完成或
`finished` 当作成功。

## 6. 已知限制

- Phoenix 是外部服务；本仓库只验证事件到 span 的映射、脱敏、截断和生命周期，不把
  “本地 Phoenix 服务可达”当作离线测试前提。
- 当前只导出 OTLP HTTP，不处理 Phoenix Cloud 的认证头；需要鉴权时应通过受控代理或
  后续扩展增加显式 header 配置。
- span 属性是查询面，不是原始 Trace 的替代品。JSONL Trace 和 Session 事实仍是审计
  事实源；Phoenix 视图可重建、可丢弃。

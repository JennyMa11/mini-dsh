# Mini-dsh 项目约束

本文件是仓库级开发约束。所有实现、测试、文档与自动化均须遵守；若设计需要突破约束，应先在 ADR 中记录原因、替代方案和迁移路径。

后续 Extension / Skills / Context Assembly / Verification / Memory 升级以
[扩展升级执行与验收主计划 v1](docs/extension-roadmap.md) 为范围和阶段验收依据。
一次只执行当前阶段；新增能力须按该计划记录范围变更，不从历史建议或参考产品自动扩充需求。
主计划不替代本文件的安全、质量与评测约束。

## 1. 项目边界

- Mini-dsh 是 Coding Agent Harness / Runtime，不训练、不微调、也不内置特定模型。
- 核心目标是可替换、可观测、可复现地执行 `Observe -> Plan -> Act -> Execute -> Observe -> Continue | Finish`。
- 首版只支持单 Agent、单工作区、单进程；多 Agent、分布式执行和图形界面不进入核心层。
- Runtime 不绑定模型厂商、Shell、代码语言、向量库或 Benchmark 数据集。
- 所有外部副作用必须经 Tool Runtime 和 Permission System；模型与 Agent Loop 不得直接访问文件、进程、网络或 Git。

## 2. 技术基线

- Python `>=3.12`，使用 `uv` 管理解释器、依赖、虚拟环境和锁文件。
- 包采用 `src/` 布局；公共 API 必须有类型标注，使用 `mypy --strict` 校验。
- 数据契约优先使用标准库 `dataclass`、`enum`、`Protocol` 和显式类型；边界输入用 Pydantic 校验。
- 测试使用 `pytest`；格式与静态检查使用 `ruff`；构建使用 `hatchling`。
- 运行时默认只依赖跨平台 Python API；平台专属行为必须隔离到 adapter。

## 3. 架构与依赖方向

建议模块边界：

```text
cli -> application(agent loop) -> domain(contracts/events)
                         |       -> ports
                         v
                  runtime/services -> adapters(model/tool/context/session)
                                      -> infrastructure(shell/fs/git/sandbox)
```

- `domain` 只包含纯契约和值对象，不依赖 adapters、CLI 或具体第三方 SDK。
- `application` 编排 Agent Loop，只依赖 ports；不得包含厂商 API、Shell 拼接或文件系统细节。
- `runtime` 负责插件注册、依赖解析、生命周期、服务注入和 Event Bus。
- `adapters` 实现 ports；厂商/平台差异只能存在于这里或 `infrastructure`。
- 禁止循环依赖；跨模块能力通过显式接口和构造函数注入。
- Model、Tool、Context、Session、Permission、Event 都必须可替换，核心 Loop 不得使用全局单例定位服务。

## 4. Agent Loop 约束

- 每次运行必须有唯一 `run_id`；每一步必须有单调递增 `step_id`。
- 状态转换必须显式、可测试，终态仅允许 `finished`、`failed`、`cancelled`、`budget_exhausted`。
- 必须设置步数、Token、墙钟时间和工具调用预算；达到预算后安全终止并记录原因。
- 模型响应不得直接产生副作用；Tool Call 必须完成解析、Schema 校验、权限判断后才能执行。
- 失败必须成为结构化 Observation 返回 Loop；不得以吞异常或只写日志代替状态处理。
- 重试只允许用于明确的瞬时错误，必须有上限和退避；有副作用的调用默认不自动重试。

## 5. 插件与服务约束

- 插件必须声明稳定 ID、版本、提供的服务、依赖及生命周期钩子。
- 注册阶段检测重复服务、缺失依赖和依赖环；启动失败按逆序释放已启动资源。
- 插件不得通过导入副作用自动注册，不得读取隐式全局配置。
- 热插拔必须发生在步骤边界；进行中的工具调用或事务不可被替换。
- Event Bus 事件必须是带版本的结构化数据；订阅者失败不得破坏核心状态一致性。

## 6. Context 约束

- Context Manager 输入输出必须可序列化，并记录策略名、版本和 Token 估算。
- 原始 Trace 是事实源；摘要、窗口和检索结果是可再生成视图，不得反向覆盖原始记录。
- 永久保留系统约束、当前任务、关键决策、未解决错误和已修改文件清单。
- 压缩或丢弃内容必须可观测，记录被压缩范围和原因。
- Full History、Sliding Window、Summary Compaction、Repo Retrieval 使用同一接口，以支持公平消融实验。

## 7. Tool Runtime 约束

- 统一执行链：`Schema -> Call -> Validation -> Authorization -> Execution -> Observation`。
- 每个工具必须声明名称、版本、输入 Schema、超时、是否有副作用和权限需求。
- Observation 至少包含状态、结构化结果或错误、耗时、截断信息和可重试性。
- 工具输出必须有大小上限；截断要显式标记，原始大输出可写入受控 artifact。
- Patch 优先于整文件覆盖；写入工作区内文件时使用原子替换或等价安全机制。
- Shell 使用参数数组执行，禁止把未经验证的模型文本直接插入 shell 字符串。

## 8. Sandbox 与权限约束

- 默认拒绝；按运行授予最小权限。规范化并校验真实路径，防止 `..`、符号链接和大小写绕过工作区。
- 命令策略同时支持 allowlist 与 denylist；deny 规则优先。
- 子进程必须设置超时，并在超时/取消时清理整个进程组。
- 环境变量采用 allowlist 传递；Secret 不得进入 Prompt、Trace、日志、测试快照或错误消息。
- 网络默认关闭，按主机、协议和端口显式授权。
- 删除、覆盖仓库外文件、提权、修改远端和其他不可逆操作必须拒绝或进入人工审批。
- Sandbox 是安全边界，不以 Prompt 约束替代操作系统级隔离。

## 9. Trace、评测与可复现性

- 所有 Model Call、Tool Call、权限决策、Context 变换、状态转换和错误都发布事件并进入 Trace。
- Trace 使用带 `schema_version` 的 JSONL；时间使用 UTC ISO 8601，时长使用单调时钟。
- Benchmark 任务必须固定输入版本、初始仓库提交、命令、预算、成功判据和隔离配置。
- 成功以可执行测试/判据为准，不以模型自报完成为准。
- 消融实验除单一变量外保持模型、采样参数、任务集、预算和环境一致。
- 指标至少包含成功率、步骤数、Token、工具调用、延迟与失败类型；本项目默认只统计 Token 用量，不计算货币费用，除非用户明确要求。保留原始 Trace 供审计。

### 9.1 分层验收与结论边界

- **Component Eval** 验证模块契约：Schema、权限、路径隔离、子进程清理、插件回滚、Context 保留等，使用契约测试和确定性故障注入。
- **Trajectory Eval** 验证执行路径：工具失败、重复失败动作、预算、恢复检查点、权限决策与 Context 变换，依据原始 Trace 和独立检查。
- **End-to-End Eval** 验证最终任务：使用独立测试、规格判据或人工审核；`finished` 只表示 Loop 正常结束，不表示任务正确完成。
- 公开题成功率只能支持对应任务集的可用性结论，不能据此宣称 Harness 的全部功能已验证。测试数量和代码覆盖率也不能替代功能验收。
- 为待验收功能维护覆盖矩阵，记录对应 case、触发条件、预期/禁止行为、检查方式、证据路径与验收状态；未触发或缺少证据的功能必须标为未测。
- 功能正确性与机制收益分别报告。宣称某机制有收益，必须进行单变量配对消融并报告重复次数、差值和不确定性；未观察到差异时如实记录。

### 9.2 Case 与证据要求

- 每个 case 固定 `case_id`、类别、任务输入、预期行为、禁止行为、预算、隔离配置、可执行判据和判据版本。阈值、规格子目标和评分口径必须在运行前声明，不得根据结果挑题或调整。
- 测试集覆盖正常、边界、工具/模型故障和安全拒绝；可按 10/5/3/2 规划初始 20 条 case，但病例数量不是验收充分条件，必须按覆盖矩阵检查缺口。
- 判据、参考解和评分标签不得进入模型可见的 Prompt、工作区或 Context；Agent 不得修改独立验证器。失败、取消、预算终止和未运行 case 不得从预先声明的任务分母中删除。
- 除 Trace 外，保留独立验证结果、必要的副作用检查与 artifact 路径。语义判据通过绑定 `run_id` 的 sidecar 记录 `case_id`、判据来源和检查结果；标签必须有可审计证据，不得使用模型自报替代。
- 关键正确性、安全和约束使用可执行规则；人工审核需记录审核范围与来源。开放质量可用经过校准的 LLM judge 辅助，不得作为关键安全项的唯一判据，也不得未经校准让同一模型生成答案并独自裁判。

### 9.3 指标及计算口径

| 指标 | 规则 |
| --- | --- |
| `task_success_rate` | 独立验证通过率；同时报告已验证数量与完整任务集结果，不把模型自报或 `finished` 视为成功 |
| `partial_success_rate` / `goal_completion_rate` | 依据运行前声明的规格子目标；部分完成不能推定为完整任务成功 |
| `constraint_satisfaction` / `constraint_satisfied_run_rate` | 分别报告逐条约束和全部约束满足的运行比例；与任务成功分开统计，明确本轮覆盖了哪些约束 |
| `tool_call_accuracy` | 工具选择和参数符合任务判据的比例，需要专项检查；Schema 有效不等于工具选得正确 |
| `tool_error_rate` | 执行失败、超时或命令非零退出 / 已执行完成的调用；参数无效和权限拒绝分别统计，不直接将失败测试归因于 Harness 缺陷 |
| `invalid_tool_call_rate` / `permission_denied_rate` | 分别统计未知工具、无效参数和权限拒绝；拒绝比例不等于安全拦截率 |
| `repeated_failed_action_rate` | 相邻同工具、同参数、同失败类型的重复失败代理指标；不得将正常复测或成功重复读取直接算作无效动作 |
| `recovery_rate` / `retry_recovery_rate` | 故障事件恢复与自动重试恢复分别报告；恢复检查必须定义事件、注入位置、期限和检查点，不能仅凭后来某次调用成功判为恢复 |
| `avg_steps` / `avg_tool_calls` | 平均 Loop 步数与工具调用数，同时给出有效样本数 |
| `latency_p50_ms` / `latency_p95_ms` | 明确计时范围、分位数算法和样本数；Agent Loop、模型、工具、Harness 开销及独立验证耗时不得混为同一口径 |
| `total_tokens` / `unknown_usage_calls` | 输入加输出，保留失败消耗；未知用量单独计数，不记为零消耗 |
| `cache_hit_rate` / `cached_input_share` | 调用缓存命中率与输入 Token 缓存占比分别报告；缓存输入是输入子集，不得再次加到总 Token |
| `unsafe_action_block_rate` / `safe_action_false_block_rate` | 禁止动作拦截率与允许动作误拦截率分别报告，必须有预定义探针及实际副作用检查 |
| `secret_leak_rate` / `policy_violation_rate` | 依据明确检查对象、检测器与规则计算；没有执行检测不能宣称零泄漏或零违规 |
| `user_acceptance_rate` / `human_escalation_rate` | 依据实际审核和介入记录，权限拒绝不能自动算作人工介入 |

- 每个比例保存分子、已检查分母、声明机会数和证据覆盖率。分母为零返回 `null`/未测，不填写 0% 或 100%；聚合合计分子、分母，不平均不同样本量的百分比。
- 未检查项保持未知。局部检查全部通过不代表完整覆盖；指标报告必须同时说明证据覆盖率、适用范围和未测功能。
- Token 用量在响应后确定，允许存在可观测的在途超量；超量全部计入消耗，达到预算后停止后续调用。记录名义额度、实际用量和超量，不能把修复成功解释为预算满足。
- 用量未知或 Trace 未结束时，完整消耗与相关约束不能判为满足；已知部分已超限时仍判违反。比较每次成功 Token 时保留失败消耗，没有成功样本则该比例未测。
- 引用准确率、证据召回、幻觉和拒答质量仅在任务确有此类目标且定义了证据/主张判据时启用，不强行加入默认 Coding 修复评分。

### 9.4 安全门禁、资源与历史基线

- 正确拒绝危险请求应按 case 的预期行为判成功，不得自动计为任务失败；同时验证合法操作没有被错误拦截。
- 关键安全探针必须独立验收：无 Secret 泄漏、无未授权副作用、超时/取消后无遗留子进程。安全失败不能由其他任务的高成功率或综合加权分抵消。
- 安全检查观察真实文件、进程、网络和输出等副作用；模拟 adapter 的测试不等同于操作系统隔离验证。不得为消融在宿主机关闭路径或权限安全边界。
- 本机评测优先轻量、原生架构、单 Agent 顺序执行；单任务容器默认 1 CPU、最多 1 GB，整轮采用预先声明的合理 Token 预算。确定性契约/故障测试优先离线运行，真实 DeepSeek 用于可用性及必要的配对消融。
- 从已有 Trace 补算指标时另存派生报告，记录原始输入与评分器版本/hash，保持原始 Trace、冻结判据和源码快照。补算不能描述为重新实测新版本，也不能据此补认未触发功能。
- 完整指标定义、sidecar 和入口见 [Harness 评测指标](docs/harness-evaluation.md)；当前轻量公开任务协议见 [轻量公开修复评测](docs/lightweight-public-benchmark.md)。

## 10. 代码与质量门禁

- 先写/更新契约测试，再实现跨边界行为；Bug 修复必须带回归测试。
- 测试分层：`unit` 不访问网络/真实 Shell；`integration` 验证 adapter；`e2e` 在临时工作区运行完整 Loop。
- 测试不得依赖真实模型服务、用户主目录、全局 Git 配置、当前时间或不固定的网络内容。
- 每次提交前必须通过：`uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy src tests`、`uv run pytest`。
- 不提交 `.env`、密钥、虚拟环境、模型响应缓存、Benchmark 工作副本或包含敏感信息的 Trace。
- 新增公共能力需更新 README 或 `docs/`；重要架构决策写入 `docs/adr/`。
- 提交保持单一目的，禁止顺手改动无关文件；生成文件必须注明来源且可再生成。

## 11. 配置与命名

- 优先顺序：CLI 参数 > 环境变量 > 配置文件 > 安全默认值。
- 环境变量统一以 `MINIDSH_` 为前缀；配置缺失时不得静默启用高权限能力。
- 对外标识使用 `snake_case`，事件名使用过去时态或事实描述，如 `tool_call_started`。
- 日志使用结构化字段，不把整个 Prompt、Secret 或无限长工具输出写入普通日志。

## 12. 交付顺序

1. 定义 domain 契约、事件与最小 Agent Loop。
2. 实现 Plugin Runtime、Service DI 和生命周期。
3. 实现 Tool Registry、结构化 Observation 与本地安全工具。
4. 实现 Permission/Sandbox，并以攻击性测试验证边界。
5. 实现 Context 策略与可替换模型 adapter。
6. 建立 Trace、Benchmark runner 和指标汇总。
7. 固定 Baseline 后进行单变量消融实验。

任何阶段都应保持主分支可运行、可测试，并提供最小端到端示例。

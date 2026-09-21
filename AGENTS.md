# Mini-dsh 项目约束

本文件是仓库级开发约束。所有实现、测试、文档与自动化均须遵守；若设计需要突破约束，应先在 ADR 中记录原因、替代方案和迁移路径。

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
- 指标至少包含成功率、步骤数、Token、工具调用、延迟、成本与失败类型；保留原始 Trace 供审计。

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

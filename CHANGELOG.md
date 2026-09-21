# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-21

首个可用版本：一个可替换、可观测、可复现的 Coding Agent Harness，附带可执行的消融实验协议。

### Added

**核心 Runtime**

- 完整的 `Observe → Plan → Act → Execute → Observe → Continue | Finish` Agent Loop，含步数、Token、工具调用与墙钟四层预算，终态限定为 `finished` / `failed` / `cancelled` / `budget_exhausted`。
- 端口驱动的依赖注入：`ModelPort` / `ContextPort` / `ToolExecutorPort` / `SessionPort` / `EventSink`，全部在 `composition.py` 一处装配。
- 插件 Runtime：稳定 ID、版本、`provides` / `requires` 声明、重复服务与依赖环检测、启动失败逆序回滚、步骤边界热替换。
- 版本化 Event Bus，订阅者失败不影响核心状态一致性。

**Context Engineering**

- 六种共用同一接口的策略：`full_history`、`sliding_window`、`summary_compaction`、`repo_retrieval`、`retrieval_summary`、`symbol_retrieval`。
- `ContextView` 自报策略名、版本、Token 估算、丢弃消息数与元数据；原始历史只读，摘要与检索均为可再生成的派生视图。
- 两个纯本地确定性检索器：词面重叠 `RepositoryRetriever` 与基于 AST 的 `PythonSymbolRetriever`，无需向量库。

**Tool Runtime**

- 统一流水线 `Schema → Validation → Authorization → Execution → Observation`。
- Read / Write / Patch / Search / Shell / Git / Test / Build 工具；Patch 优先于整文件覆盖；写入使用原子替换。
- 工具输出大小上限（默认 20 000 字符）、显式截断标记与 `LocalArtifactStore` 落盘。
- 结构化 Observation：状态、结果或错误、耗时、截断信息、可重试性；被拒绝的调用同样发布事件。

**安全模型**

- `WorkspaceSandbox`：拒绝绝对路径、真实路径归属校验、符号链接防逃逸、不展开 `~`、原子写入校验父目录。
- `CommandPolicy`：deny-first（`rm` / `sudo` / `ssh` / `scp`）、显式 allowlist、argv 数组不经过 Shell、空 argv 拒绝。
- `NetworkPolicy`：默认 deny-all，按 `(host, protocol, port)` 三元组授权。
- 密钥文件拒绝（`.env` / `.pem` / `.key` / `.p12` / `.ssh` / `id_rsa` 等），错误消息不回显内容。
- 子进程环境变量 allowlist；超时后 `killpg` 清理整个进程组。
- Trace 落盘前对 Secret 键、`Bearer` 头与 `key=value` 赋值二次脱敏。
- 可选 `DockerCommandRunner`：只读根文件系统、`cap-drop ALL`、`no-new-privileges`、PID/内存/CPU 上限、`/tmp` noexec、默认 `--network none`。

**配置**

- 分层优先级 `CLI > MINIDSH_* 环境变量 > minidsh.toml > 安全默认值`，可打印每个字段的来源。
- 类型转换失败显式报错，未知或越界字段被拒绝；`max_seconds` 作为 `max_duration_seconds` 的兼容别名。

**Benchmark 与消融**

- 版本化 manifest（`schema_version` / `repo_commit` / `seed` / `model` / `budget` / `isolation` / `success_argv`），路径相对 manifest 解析。
- 隔离任务副本、独立进程执行可执行判据、JSONL 结果与逐任务 Trace 持久化。
- 指标：成功率、步数、Token、工具调用、延迟、成本、重试成功率、权限拒绝数、危险动作拦截数、失败类型。
- `AblationRunner` 与四组内置 suite：`standard`、`context`、`tool_output`、`permission`，单变量对照。
- 29 个任务：`smoke`（1）、`core`（25，覆盖六类任务）、`stress`（3，长历史 / 大输出 / 被拒绝动作）。

**模型适配器**

- `ScriptedModel`：确定性响应序列，未声明 Token 时测量实际上下文，支持从文件加载。
- `OpenAICompatibleModel`：只从显式配置读取凭据，含工具 Schema 传递与非法参数处理。

**工程与文档**

- `ruff format` / `ruff check` / `mypy --strict` / `pytest` 四项门禁，CI 覆盖；112 个测试分 `unit` / `integration` / `e2e` 三层。
- 最小端到端示例 `examples/minimal_demo.py` 与一键脚本 `scripts/run-{benchmark,ablation,all-experiments}.sh`。
- 文档：README（架构图、Loop 流程图、Benchmark 方法、真实结果表）、`docs/benchmark.md`、`docs/context-engineering.md`、`docs/sandbox-security.md`、`docs/technical-highlights.md`、6 条 ADR。

### Measured results

环境：`scripted` 模型、seed `0`、$3 / $15 per million tokens。

| 实验 | 读数 |
| --- | --- |
| `smoke` 基线 | 1 任务 / 100% / 208.0 Token / $0.001080 |
| `core` 基线 | 25 任务 / 100% / 749.9 Token / $0.064545 |
| `stress` 基线 | 3 任务 / 100% / 9534.0 Token / $0.095022 / denied=1 / blocked=4 |
| Context 消融（stress） | `sliding_window` 8981.0 vs `repo_retrieval` 12617.0 Token（+40%） |
| 输出管理消融（stress） | `unbounded` 32136.0 vs `bounded` 9511.3 Token（3.38×） |
| 权限消融（stress） | `basic_feedback` 8622.7 vs `detailed_feedback` 9534.0 Token |

同一命令重复运行得到逐字节相同的 Token 与成本读数：`AgentLoop` 刻意不把墙钟 `duration_ms` 写入模型可见的反馈（它只进事件流），否则变长浮点会让 Token 计数在运行间漂移。

### Known limitations

- `LocalCommandRunner` 是策略层而非容器/内核级隔离；不可信模型与仓库必须使用 Docker 后端或外层 VM。
- 脚本化模型下成功率恒为 100%，因此当前实验只能得出成本与行为结论，不能得出准确率结论。
- `symbol_retrieval` 目前只支持 Python。
- 单 Agent、单工作区、单进程；多 Agent 与分布式执行不在核心层范围内。

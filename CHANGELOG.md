# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-09-21

**Real-model evaluation release。** `0.1.0` 证明了 Harness 按规格工作；`0.2.0` 补上"在真实模型上做实验"所需的全部工程：可复现的 provider 接入、重复采样、成本与延迟分解、失败分类、Trace 回放与差分、以及报告里的 Pareto trade-off。

### Added

**真实模型接入**

- `ModelProfile` 与四个内置 profile：`deepseek`、`deepseek-reasoner`、`openai`、`ollama`，声明 endpoint / model / 凭据变量 / 上下文窗口，支持 `MINIDSH_MODEL_PROFILE` 与逐字段 `--endpoint` / `--model` / `--api-key-env` 覆盖。
- **价目不内置**：真实 provider 的 `pricing` 为 `None`，未声明价目时拒绝运行（而不是报告 `$0`）。零价目与未声明价目不可区分，因此"是否已声明"被显式跟踪；`--allow-unpriced` 是显式逃生口。
- `OpenAICompatibleModel` 强化：429/5xx/超时/网络错误有限重试与指数退避（上限可配，`sleep` 可注入以便测试）；HTTP 失败分类为 `auth_error` / `rate_limited` / `server_error` / `client_error`；`ModelAdapterError` 带 `code` 与 `retryable`，失败分类不再依赖解析错误文本。
- Prompt cache 支持：解析 `usage.prompt_tokens_details.cached_tokens`，`TokenUsage` 新增 `cached_input_tokens` 与 `billable_input_tokens`。

**评测层（新包 `minidsh/evaluation/`）**

- `tracefile.py`：JSONL Trace 的类型化只读视图，按 step 分组，导出状态、失败原因、Token 与成本。Trace 是唯一事实源，其余全部是可再生成的视图。
- `taxonomy.py`：9 个失败类别归并成 4 层（`success` / `harness` / `agent` / `environment`），带证据链的 `FailureDiagnosis`。决策顺序先测无歧义的结构化事实，再对模型做推断；"被绕过的拒绝不算病因"与"`context_miss` 需要两半证据"是两条关键规则。
- `latency.py`：`model_ms` / `tool_ms` / `harness_ms` 三层分解与按工具的延迟聚合。
- `pareto.py`：Success Rate vs Tokens / Cost / Steps 的 Pareto 前沿、被支配变体及其支配者与差值；全并列时明确报告"这一轴无法区分"，而不是伪装成 trade-off。
- `replay.py`：不重新调用模型的逐步回放，按 Loop 自身词汇输出 `Context → Model → Tool Call → Observation → State`。
- `diff.py`：对齐两条 Trace 的逐步差分，定位第一个行为分叉点，给出 Context 差、Token 差与工具调用分叉原因。

**Benchmark**

- `BenchmarkRunner` 支持 `--repeat N`（逐任务交错采样，避免 provider 抖动在整个套件上相关）与 `model_label` 覆盖；`TaskMetrics` 新增 `run_index`、`cached_input_tokens`、`failure_category` / `failure_layer` / `failure_reason` 与三项延迟分量。
- 聚合指标新增 `runs`、`pass_at_k`、`cost_per_success_usd`、`cached_input_share`、三项平均延迟与 `repeat_variance`（`success_spread` / `token_spread`）。
- 报告新增 "Outcome by layer" 层分解表与失败类别 / 原始失败类型两张分布表。
- `AblationReport` 新增 `points()` / `pareto()`，Markdown 追加完整的 Pareto trade-off 章节，JSON 内嵌 `pareto` 结论。
- 新增 `repo` 套件（6 个任务，无 response script，每个跨至少两个模块）：跨文件 Bug Fix、定义+调用点契约、测试失败定位、新功能+测试、配置修改、多轮错误恢复。生成脚本 `benchmarks/build_repo.py` 固定每个 fixture 的 `sha256:` 摘要。
- `JsonlTraceWriter` 新增 `truncate_on_start`：writer 自己拥有文件生命周期，调用方不再需要先删除旧 Trace。

**CLI**

- `benchmark` / `ablation` 新增 `--provider` / `--model-profile` / `--model` / `--endpoint` / `--api-key-env` / `--temperature` / `--max-attempts` / `--repeat` / `--allow-unpriced` / `--cached-input-usd-per-million`。
- `mini-dsh report <json>`：离线重渲染已保存报告并附 Pareto 分析，不重新调用模型。
- `mini-dsh trace replay <jsonl>` 与 `mini-dsh trace diff <a> <b>`，均支持 `--json`。

**文档**

- 新增 [`docs/real-model-evaluation.md`](docs/real-model-evaluation.md)：profile 表、成本模型、延迟模型、重复采样协议、套件选型、结果读法，以及 `3.38×` / `+40%` / `−27%` 三个数字的 workload / baseline / variant / metric / run count / 是否真实模型六项来源说明。
- 新增 [`docs/failure-taxonomy.md`](docs/failure-taxonomy.md)：类别表、层归属、决策顺序、诊断证据格式、Trace 回放与差分工作流、失败案例分析流程。

### Changed

- `ModelPricing` 新增 `cached_input_usd_per_million`；未声明时回落到未缓存费率，**与旧成本模型逐位等价**（`0.1.0` 的全部成本读数不变）。
- `AgentLoop` 在 `model_call_completed` 中发布 `duration_ms` 与 `cached_input_tokens`，`run_finished` 中发布 `latency` 分解；`RunResult` 新增 `latency` 字段。
- `BenchmarkRunner` 不再删除旧 Trace 文件，改为由 writer 截断；Trace 路径在 `--repeat > 1` 时为 `<task>.run<N>.jsonl`（第 0 次仍为 `<task>.jsonl`，保持既有产物路径）。
- 配置层新增 `MINIDSH_CACHED_INPUT_USD_PER_MILLION`。
- 环境变量 `--api-key-env` 默认值由固定字符串改为"沿用 profile 声明的变量"。

### Fixed

- 真实 provider 的 HTTP 失败此前统一抛出无结构的 `ModelAdapterError`，且 5xx/429 不会重试；现在按状态码分类并只对瞬时错误重试。
- 旧 Trace 未被清理时会与新运行的事件交错；现在由 writer 在首次写入时截断。
- **失败的模型调用其耗时此前不计入任何一层**，于是被算进 `harness_ms` 残差——provider 越慢越像"harness 慢"，正好与延迟分解的目的相反。修复覆盖两处：Loop 现在把失败与被取消的调用也计入 `model_ms` / `model_calls`；`latency_from_trace` 改为优先采用 `run_finished` 上由 Loop 记录的权威分解，只在旧 Trace 缺该字段时才回退到按事件求和（失败的调用没有 `model_call_completed` 事件，按事件求和必然丢时间）。这个缺陷是在用本地 `ollama` profile 做端到端探测时暴露的：连接失败的 3.5 秒被全部记到了 harness 头上。

### Tests

- 185 个测试（+54）：新增 `tests/test_evaluation.py`（29 个）覆盖 Trace 解析、失败分类的每一条分支与决策优先级、延迟分解、Pareto 前沿与并列、回放与差分；`tests/test_docs.py`（9 个）把 README 与 `docs/` 上的版本、模块数、任务数与测试数变成**由仓库推导的断言**，任一数字手改失配即门禁失败；`tests/test_agent_loop.py` 新增"失败的模型调用必须记在模型账上而非 harness 账上"的回归测试；`tests/test_integration.py` 新增 429→503→200 重试成功、401 不重试、prompt cache 解析；`tests/test_benchmark.py` 新增重复采样与层归属；`tests/test_cli.py` 新增 report / replay / diff 与 profile 拒绝路径。

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

- `ruff format` / `ruff check` / `mypy --strict` / `pytest` 四项门禁，CI 覆盖；131 个测试分 `unit` / `integration` / `e2e` 三层，覆盖率 83%。
- `runtime/` 覆盖率为 100%：插件重复 ID、重复服务提供者、依赖环、声明未注册、启动回滚、回滚失败、聚合关停失败、热替换三类失败路径全部有测试守住。
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

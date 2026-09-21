# Mini-dsh

Mini-dsh 是一个轻量级、可扩展、可评测的 Coding Agent Harness / Agent Runtime。它实现完整的 `Observe → Plan → Act → Execute → Observe → Continue | Finish`，重点研究在模型不变时，Context、工具反馈、反思、权限和执行隔离如何改变任务成功率与成本。

它不是一个新模型，也不是一个产品级 Coding Agent。它是一台**可替换、可观测、可复现**的 Harness：模型、工具、Context、Session、权限与事件总线全部走端口，核心 Loop 不依赖任何厂商 SDK、Shell 或文件系统细节，因此可以拿来做单变量消融实验。

- 版本：`0.3.1`（Apache-2.0）
- 运行时：Python `>=3.12`，依赖仅 `pydantic`，用 `uv` 管理
- 规模：40 个源码模块、14 个测试模块、203 个测试、31 个 Benchmark 任务（`core` 25 + `repo` 6）

> `0.2.0` 是 **Real-model evaluation release**：接入真实 provider profile、重复采样、成本与延迟分解、失败分类、Trace Replay / Diff 与 Pareto 分析。`0.3.0` 交付**第一批真实模型实测结果**：`repo` 套件在 `deepseek-flash` 上 18/18 通过、每次成功 $0.000937、Harness 自身占墙钟 0.26%，并修掉了接入时暴露的三个 provider 缺陷。scripted 基线仍然可复现，但"模型能力"结论只能来自 `repo` 套件。

---

## Architecture

```mermaid
flowchart TD
    CLI["cli.py<br/>run / benchmark / ablation<br/>report / trace replay / trace diff"]
    COMP["composition.py<br/>Composition Root · RuntimeOptions"]
    LOOP["application/agent_loop.py<br/>Observe → Plan → Act → Execute"]

    subgraph PORTS["ports/ · domain/（纯契约，无第三方依赖）"]
        P1["ModelPort"]
        P2["ContextPort"]
        P3["ToolExecutorPort"]
        P4["SessionPort"]
        P5["EventSink"]
    end

    subgraph RUNTIME["runtime/"]
        PM["plugins.py<br/>依赖校验 · 生命周期 · 回滚"]
        SR["services.py<br/>Service DI"]
        EB["events.py<br/>版本化 Event Bus"]
    end

    subgraph TOOLS["tools/"]
        TR["runtime.py<br/>Schema → Validation →<br/>Authorization → Execution → Observation"]
        BU["builtin.py<br/>Read Write Patch Search Shell Git Test Build"]
        PP["PermissionPolicy<br/>deny 优先 · 能力授予"]
    end

    subgraph ADAPTERS["adapters/ · infrastructure/"]
        AM["models.py<br/>Scripted / OpenAI-compatible"]
        AP["profiles.py<br/>Provider Profile · 重试 · 缓存计价"]
        AT["trace.py<br/>脱敏 JSONL"]
        AS["sessions.py<br/>JSON Session"]
        SB["sandbox.py<br/>WorkspaceSandbox · NetworkPolicy"]
        PR["process.py<br/>Local / Docker Runner"]
    end

    subgraph EVAL["evaluation/（只读派生视图）"]
        ET["taxonomy.py<br/>失败分类与层归属"]
        EL["latency.py<br/>model / tool / harness"]
        EP["pareto.py<br/>trade-off 前沿"]
        ER["replay.py · diff.py<br/>逐步回放与差分"]
    end

    CLI --> COMP --> LOOP
    LOOP -.依赖.-> PORTS
    COMP --> RUNTIME
    COMP --> TOOLS
    PORTS -.实现.-> ADAPTERS
    TOOLS --> ADAPTERS
    PM --> SR
    TR --> PP
    AT -.读取.-> EVAL
    CLI --> EVAL
```

依赖方向单向收敛：`cli → composition → application → domain/ports → runtime/adapters`。`domain` 只有不可变 dataclass、`enum` 和 `Protocol`，不 import 任何 adapter；所有外部副作用（文件、进程、网络、Git）都必须穿过 Tool Runtime，模型与 Loop 不能直接触达。完整取舍见 [`docs/adr/0001-runtime-architecture.md`](docs/adr/0001-runtime-architecture.md)。

## Agent Loop

```mermaid
stateDiagram-v2
    [*] --> Running
    Running --> Running: 模型调用 + 工具执行<br/>step_id += 1
    Running --> Finished: 模型给出最终回答<br/>且验证判据通过
    Running --> Failed: 不可恢复错误
    Running --> Cancelled: 外部取消
    Running --> BudgetExhausted: 步数 / Token / 工具调用 / 墙钟耗尽
    Finished --> [*]
    Failed --> [*]
    Cancelled --> [*]
    BudgetExhausted --> [*]
```

一次迭代的固定顺序：

```text
Observe  → ContextManager 依据策略构造视图（记录策略名、版本、Token 估算）
Plan     → ModelPort.complete(view) 返回回答或工具调用
Act      → 每个 tool_call 先经 Schema 校验，再经权限判断
Execute  → Tool Runtime 执行，产出结构化 Observation（状态/结果/耗时/截断/可重试）
Observe  → Observation 回灌 Context，失败以结构化事实返回而非吞异常
Continue → 未达终态且预算未耗尽则回到 Observe；否则进入唯一终态
```

不变量：每次运行唯一 `run_id`，每步单调递增 `step_id`；终态只有 `finished` / `failed` / `cancelled` / `budget_exhausted`；模型输出本身不产生副作用；重试只针对显式标记的瞬时错误且默认不重试有副作用的调用。

## 已实现能力

- 带 Step、Token、工具调用和墙钟预算的 Agent Loop，以及明确的终态。
- Model、Context、Tool、Event、Session 端口；插件依赖校验、生命周期回滚、Service DI 和热替换边界。
- Full History、Sliding Window、Summary Compaction、Repo Retrieval、Retrieval+Summary、Python Symbol Retrieval 六种 Context 策略，共用同一接口以支持公平消融。
- Read、Write、Patch、Search、Shell、Git、Test、Build 工具与统一 Schema/授权/超时/重试/Observation 流程。
- 工具输出大小上限、显式截断标记与 artifact 落盘（大输出不污染 Context）。
- 工作区路径与符号链接防逃逸、命令 deny/allowlist、环境过滤、敏感文件拒绝、进程组超时清理。
- 网络策略按 host / protocol / port 显式授权，默认全关。
- 可选 Docker hardened runner：只读根文件系统、capability drop、资源限制和默认断网。
- Scripted 与 OpenAI-compatible 模型适配器、JSON Session、脱敏 JSONL Trace。
- 隔离任务副本、可执行成功判据、指标汇总、成本核算和消融实验 API。
- 命名 Provider Profile（DeepSeek / OpenAI / Ollama）与带指数退避的瞬时错误重试；prompt cache 命中单独计价。
- 失败分类法：9 个类别归并成 `success` / `harness` / `agent` / `environment` 四层，每个诊断带证据链。
- 三层延迟分解（model / tool / harness）、cost per successful run、重复采样与 `pass@k`、Pareto trade-off 分析。
- Trace Replay（不调用模型，逐步重建 `Context → Model → Tool Call → Observation → State`）与 Trace Diff（定位两条 Trace 的第一个行为分叉点）。
- 分层配置：CLI > `MINIDSH_*` 环境变量 > `minidsh.toml` > 安全默认值，并可打印每个字段的来源。

## 快速开始

需要 Git 和 [`uv`](https://docs.astral.sh/uv/)，Python 3.12 与依赖由 `uv` 自动管理：

```bash
uv sync --all-groups
uv run mini-dsh doctor
uv run pytest
```

无需 API Key 的最小端到端示例（临时工作区、脚本化模型、真实 Patch 与测试）：

```bash
uv run python examples/minimal_demo.py
```

输出示例：

```text
status: finished
steps: 4
tool calls: 3
tokens: 986
cost: $0.004110
```

## 运行 Coding Agent

Scripted provider 用于可复现实验：

```bash
uv run mini-dsh run "Fix the bug and run tests" \
  --workspace /path/to/repo \
  --script /path/to/responses.json \
  --allow-write \
  --allow-shell \
  --allow-command python \
  --test-command "python -m pytest"
```

OpenAI-compatible provider 通过命名 profile 选择；凭据只从 profile 声明的环境变量读取：

```bash
export DEEPSEEK_API_KEY=...

uv run mini-dsh run "Implement the requested change" \
  --provider openai-compatible \
  --model-profile deepseek \
  --model deepseek-flash \
  --workspace /path/to/repo \
  --context retrieval_summary \
  --input-usd-per-million 0.0 \
  --output-usd-per-million 0.0 \
  --allow-write
```

真实 provider 未声明价目时会拒绝运行，详见[真实模型评测](#真实模型评测)。

对不可信仓库启用 Docker 后端（需要宿主机已安装 Docker，生产环境应把镜像固定到 digest）：

```bash
uv run mini-dsh run "Fix the failing tests" \
  --provider openai-compatible \
  --workspace /path/to/repo \
  --allow-write \
  --allow-shell \
  --allow-command python \
  --sandbox-backend docker \
  --docker-image python:3.12-slim
```

默认只注册 Read、Search 和只读 Git；Write 与 Shell 必须逐次显式开启。Shell 中每个可执行文件还必须通过 `--allow-command` 或固定 test/build 命令加入 allowlist。运行结果输出最终回答、Git diff、指标及 Trace 路径。

## Benchmark 方法

一条命令跑完全部基线、消融与结果归档：

```bash
./scripts/run-all-experiments.sh          # 全部
./scripts/run-benchmark.sh core           # 单套基线
./scripts/run-ablation.sh stress context  # 单组消融
```

方法要点（完整协议见 [`docs/benchmark.md`](docs/benchmark.md)）：

1. **任务集固定**：每个任务声明 `repo_commit`、`seed`、`model`、`budget`、`isolation` 与成功判据 argv；manifest 与 fixture 全部入库，可离线复现。
2. **隔离副本**：Runner 为每个任务复制临时工作区，Agent 在副本上操作，互不污染。
3. **判据可执行**：成功与否由独立 `success_argv` 的退出码决定，从不采信模型自报完成。
4. **单变量消融**：`AblationRunner` 对同一 manifest 依次运行 `HarnessVariant`，只改一个变量（Context 策略 / 输出上限 / 重试与反馈粒度），模型、任务、预算与环境保持不变。
5. **事实优先**：每次 Model Call、Tool Call、权限决策、Context 变换与状态转换都发布版本化事件并落 JSONL Trace；原始 Trace 是事实源，摘要是可再生成视图。
6. **成本口径统一**：`ModelPricing` 显式给定 `input_usd_per_million` / `output_usd_per_million`，Token 由 Harness 侧确定性估算，报告给出每任务与总成本。

任务集规模：

| Suite | 任务数 | 模型 | 覆盖 |
| --- | --- | --- | --- |
| `smoke` | 1 | scripted | 端到端链路自检 |
| `core` | 25 | scripted | Bug Fix / Feature / Test Repair / Refactor / Code Understanding / Multi-file |
| `stress` | 3 | scripted + **provider** | 长历史（18 步）、大输出（75 KB 日志）、被拒绝动作（越权 + 逃逸 + 密钥读取） |
| `repo` | 6 | **provider** | 跨文件 Bug Fix、定义+调用点契约、测试失败定位、新功能+测试、配置修改、多轮错误恢复 |

`repo` 套件没有 response script：每个任务至少跨两个模块，且其中几个 fixture 被刻意构造成"第一反应的那个改法会让另一个测试挂掉"，用来逼出多轮错误恢复而不是一次蒙对。生成脚本是 [`benchmarks/build_repo.py`](benchmarks/build_repo.py)。

`stress` 同时用于 scripted 与真实 provider 的消融：它的任务足够长，长历史与大输出两组变量才有区分度（`core` 上这两组对照被压平）。

## 实验结果

### scripted 基线（Harness 正确性）

环境：`scripted` 模型（确定性，无网络）、seed `0`、定价 $3 / $15 per million tokens（输入 / 输出）。原始报告与 Trace 归档在 [`docs/results/`](docs/results/)，汇总见 [`docs/results/summary.json`](docs/results/summary.json)。

**基线**

| Suite | 任务 | 成功率 | 平均步数 | 平均 Token | 总成本 | 拒绝 | 拦截 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `smoke` | 1 | 100% | 4.00 | 208.0 | $0.001080 | 0 | 0 |
| `core` | 25 | 100% | 4.00 | 749.9 | $0.064545 | 0 | 0 |
| `stress` | 3 | 100% | 10.00 | 9534.0 | $0.095022 | 1 | 4 |

**Context 策略消融**（`stress`，只改 Context 策略）

| 策略 | 平均 Token | 总成本 | 成功率 |
| --- | --- | --- | --- |
| `sliding_window` | 8981.0 | $0.090045 | 100% |
| `summary_compaction` | 9233.7 | $0.092319 | 100% |
| `symbol_retrieval` | 9233.7 | $0.092319 | 100% |
| `full_history` | 9534.0 | $0.095022 | 100% |
| `retrieval_summary` | 12317.0 | $0.120069 | 100% |
| `repo_retrieval` | 12617.0 | $0.122769 | 100% |

**工具输出管理消融**（`stress`，只改输出上限与 artifact 落盘）

| 变体 | 平均 Token | 总成本 | 成功率 |
| --- | --- | --- | --- |
| `unbounded_output` | 32136.0 | $0.298440 | 100% |
| `bounded_output` | 9511.3 | $0.094818 | 100% |
| `bounded_output_artifact` | 9534.0 | $0.095022 | 100% |

**权限反馈与重试消融**（`stress`，只改重试策略与反馈粒度）

| 变体 | 平均 Token | 总成本 | 成功率 |
| --- | --- | --- | --- |
| `basic_feedback` | 8622.7 | $0.086820 | 100% |
| `no_retry` | 9534.0 | $0.095022 | 100% |
| `single_retry` | 9534.0 | $0.095022 | 100% |
| `detailed_feedback` | 9534.0 | $0.095022 | 100% |

**怎么读这些数**

- **Token 差异是可信的**：Harness 自己测量送入模型的 Context，脚本化响应不预置 Token 数；同一任务换策略即换输入。同一命令重复运行得到逐字节相同的读数。
- **成本差异直接跟随 Token**：`unbounded_output` 比 `bounded_output` 贵 3.38 倍，说明输出上限是成本杠杆而不是正确性杠杆。
- **检索类策略在长历史上更贵**：`repo_retrieval` 比 `sliding_window` 多 40% Token，因为它把相关文件重新注入 Context；`symbol_retrieval` 因为只切 AST 符号体，比 `repo_retrieval` 便宜 27%。
- **成功率恒为 100% 是设计使然**：脚本化模型保证步骤可预测，这一列校验的是 Harness 链路完整性（Patch 生效、判据可执行、状态机不误判），不是模型能力。要测能力必须换成真实 provider 并接受成功率波动——真实 provider 下的实测见下一节。
- **`denied=1 / blocked=4`** 只在 `stress` 出现，正是 `stress-denied-action` 的设计意图：越权写入、`curl`、`rm -rf`、密钥读取与 `../` 逃逸都被策略层拦下并计入统计。
- **`core` 上 `tool_output` 消融无差异**（三个变体同为 749.9 Token）：该套件没有大输出任务，这组对照在 `core` 上不可分辨——这正是必须造 `stress` 套件的原因。
- **`stress-denied-action` 的 `denied=1 / blocked=4` 不会让它被归类为失败**：被拒绝的动作随后被合法工具绕过了，因此分类法把它判为 `success` 层。拒绝是"Harness 按规格工作"的证据，不是病因——这条规则见 [`docs/failure-taxonomy.md`](docs/failure-taxonomy.md)。

### 真实模型基线（`repo` 套件）

上面整张表都是 Harness 正确性的证据，不是模型能力的证据。下表是同一套 Harness 换成真实 provider 后的实测值。原始报告与 Trace 归档在 [`docs/results/repo-real.md`](docs/results/repo-real.md)，机器可读汇总见 [`docs/results/summary.json`](docs/results/summary.json) 的 `real_model` 块。

<!-- real-model-results:start -->
条件：`deepseek-flash` · thinking 关闭（`temperature_effective: true`，即 `temperature=0` 真正生效）· 每任务重复 3 次（逐任务交错采样）· off-peak 费率 $0.15 / $0.6 / $0.003 per 1M tokens（输入 / 输出 / 缓存命中）

| 指标 | 实测值 |
| --- | --- |
| 成功率 | 100%（18/18） |
| `pass@k` | 100% |
| 平均步数 | 6.61 |
| 平均 Token | 19,132 |
| 缓存输入占比 | 86% |
| 平均延迟 | 6,410.1 ms（model 6,290.6 / tool 103.1 / harness 16.4） |
| 总成本 | $0.016865 |
| 每次成功成本 | $0.000937 |
| 被拦截的危险动作 | 20 |
<!-- real-model-results:end -->

失败层分解是 `success 18 / harness 0 / agent 0 / environment 0`；`repeat_variance` 的成功率极差 `0.00`，Token 极差 15%。

**这个 100% 只说明这么多**：

- **样本极小，且不是独立评测。** 6 个任务 × 3 次 = 18 次运行；任务由写 Harness 的同一个人构造。它能排除的是"多轮工具循环根本跑不起来"这类整体性失败（见下一条），不能用来论证模型在真实仓库上的能力。
- **它顺带排除了一个真实的整体性失败**：DeepSeek 上游默认开启 thinking，而 thinking 模式下 API 会忽略 `temperature`、且要求历史 `reasoning_content` 原样回传（否则 400）。这个缺陷是第一次真实运行时才暴露的，修完之后 18 次运行全部通过。
- **三个与成功率无关、但只有真实 provider 才能测到的数字**：Harness 自身只占墙钟的 **0.26%**（16.4 ms / 6,410.1 ms），其余全是 provider 往返；**86% 的输入 Token 命中 prompt cache**，这是长上下文策略在生产中是否付得起的决定性数字；**每次成功成本 $0.000937**，比"更便宜但更容易失败"的变体更有决策价值。
- **20 次拦截全部是 `ls` / `find` 未进 allowlist**（本次只放行了 `python`）。按分类法它们落在 `success` 层：Agent 改用 `read_file` / `search` 绕过后仍然全部通过。这既实测了"拒绝可观测"，也说明这次运行里 Agent 少了它本来会伸手去拿的 shell 命令——一个诚实但可控的配置代价。
- **完整性检查**：18 条 Trace 里每次运行的写入都落在目标源码文件上（`shop/pricing.py`、`textkit/normalize.py` + `textkit/slugify.py` 等），**对任何测试文件的写入为 0**。成功率不是靠改测试拿到的。

### 真实模型消融（`stress` 套件）

scripted 消融能证明"策略之间有成本差异"，但**不能**证明"某个策略更准"——脚本化模型成功率恒为 100%，那一列没有区分度。把同一套消融换到真实 provider 上，第一次就暴露了一个 harness 缺陷（见本节末），修完之后才拿到可解释的对照。

报告归档在 [`docs/results/stress-real-context-ablation.md`](docs/results/stress-real-context-ablation.md) 与 [`docs/results/stress-real-tool_output-ablation.md`](docs/results/stress-real-tool_output-ablation.md)，机器可读汇总见 [`docs/results/summary.json`](docs/results/summary.json) 的 `real_model_ablations` 块。

<!-- real-model-ablation-results:start -->
条件：`deepseek-flash` · thinking 关闭 · 3 个 `stress` 任务 × 3 次重复 = 每变体 9 次运行 · off-peak 费率 $0.15 / $0.6 / $0.003 per 1M tokens（输入 / 输出 / 缓存命中）

**Context 策略消融**

| 变体 | 成功率 | 平均 Token | 平均步数 | 缓存占比 | 总成本 | 每次成功成本 |
| --- | --- | --- | --- | --- | --- | --- |
| `full_history` | 100% | 29,315 | 8.33 | 92% | $0.011553 | $0.001284 |
| `sliding_window` | 100% | 26,096 | 8.56 | 85% | $0.012579 | $0.001398 |
| `summary_compaction` | 100% | 26,073 | 8.22 | 83% | $0.013322 | $0.001480 |
| `repo_retrieval` | 100% | 17,767 | 6.56 | 80% | $0.011184 | $0.001243 |
| `retrieval_summary` | 100% | 16,747 | 6.33 | 75% | $0.011803 | $0.001311 |
| `symbol_retrieval` | 100% | 29,331 | 7.56 | 77% | $0.016193 | $0.001799 |

**工具输出消融**

| 变体 | 成功率 | 平均 Token | 总成本 | 每次成功成本 |
| --- | --- | --- | --- | --- |
| `unbounded_output`（上限 200K） | 100% | 32,687 | $0.011849 | $0.001317 |
| `bounded_output`（上限 20K） | 100% | 31,986 | $0.011743 | $0.001305 |
| `bounded_output_artifact`（上限 20K + 溢出落盘） | 100% | 30,523 | $0.011148 | $0.001239 |
<!-- real-model-ablation-results:end -->

**结论与限制**：

- **成功率仍然是 100%，所以这一列依然没有区分度。** Context 消融 6 变体 × 9 次 = 54 次、工具输出消融 3 × 9 = 27 次，全部通过，失败层分解全部是 `success 9`。`stress` 套件对 `deepseek-flash` 太容易——**这组实验只能得出成本结论，不能得出"某策略更准"的结论**，和 scripted 消融是同一个限制，只是现在发生在真实模型上。
- **Token 更少 ≠ 更便宜，在缓存计价下会反号。** `sliding_window`（26,096）与 `summary_compaction`（26,073）都比 `full_history`（29,315）省约 11% Token，但**都更贵**（$0.012579 / $0.013322 vs $0.011553）。机制是 prompt cache：命中价 $0.003 与未命中价 $0.15 相差 **50 倍**，而截断策略每一步都在改动前缀、把可复用前缀打断；`full_history` 只追加不修改，因此拿到最高的 92% 命中率。**这是"用 Token 数当成本代理"会系统性出错的地方。**
- **`repo_retrieval` 是成本赢家**（$0.011184，唯一在成本轴上不被支配的变体），步数也接近最少（6.56）：它在"多注入一点 Token"与"保住缓存前缀"之间取到了最好的平衡。
- **`retrieval_summary` 在 Token（16,747）、步数（6.33）、延迟（6,826 ms）三项上都是最少**，但成本高于 `repo_retrieval`——它的缓存命中率是六个里最低的 75%，因为摘要消息每步都要重算。
- **`symbol_retrieval` 最贵**（$0.016193）：Token 数与 `full_history` 几乎相同（29,331 vs 29,315），成本却高 40%，同样是缓存命中率只有 77%。
- **工具输出消融里真正有效的杠杆是落盘，不是上限。** `bounded_output_artifact` 在 Token、成本、步数三轴都不被支配：相比不设上限省 6.6% Token、5.9% 成本且成功率不变；而单把上限从 200K 压到 20K 只省 2.1%。
- **harness 自身开销 18.9–27.1 ms，占墙钟 0.2–0.4%**，六个 Context 变体一致，说明上表的差异不是 harness 侧造成的。

**这次消融是先修了一个 bug 才成立的。** 第一版跑出来四个变体成功率 0–22%，且全部记成 `provider_error`——而 `provider_error` 落在 `environment` 层，读起来就是"provider 挂了"。实际原因是截断型策略把一次工具调用回合从中间切开：保留了工具结果，却丢掉了声明它的那一轮 assistant 消息，provider 因此返回 400。**脚本模型不校验它收到的请求，所以这个缺陷在 deterministic 阶段结构性不可见**——deterministic 基线跑多少遍都照不出来。修复、回归测试与协议约束见 [`CHANGELOG.md`](CHANGELOG.md) 的 `0.3.1` 与 [`docs/context-engineering.md`](docs/context-engineering.md) §3.1；上表是修复后重跑的结果，修复前那版数字不作为 Context 结论。

## 真实模型评测

scripted 套件回答的是"Harness 是否按承诺工作"，不是"模型是否够好"。前者可以 100%，后者必须真实 provider 才能测。完整协议见 [`docs/real-model-evaluation.md`](docs/real-model-evaluation.md)。

**命名 Provider Profile**（pricing 故意不内置：官方价目会变，过期价目会静默污染报告里的每一个成本数字）：

| Profile | Endpoint | 凭据变量 | 价目 |
| --- | --- | --- | --- |
| `deepseek`（`deepseek-flash`） | `api.deepseek.com/chat/completions` | `DEEPSEEK_API_KEY` | 必须显式声明 |
| `deepseek-v4-pro` | `api.deepseek.com/chat/completions` | `DEEPSEEK_API_KEY` | 必须显式声明 |
| `openai` | `api.openai.com/v1/chat/completions` | `OPENAI_API_KEY` | 必须显式声明 |
| `ollama` | `127.0.0.1:11434/v1/chat/completions` | 无需 | 已声明为免费 |

**DeepSeek 的 thinking 默认被关掉**，这不是省事而是为了可复现：thinking 模式下 API 会**忽略 `temperature`**，所以开着它 `--temperature 0` 就形同虚设。要用 `--thinking` 显式打开，harness 会同时把 `reasoning_content` 在每次带 `tools` 的请求里回传（不回传 API 会直接返回 400）。

```bash
export DEEPSEEK_API_KEY=...

# 单任务 + 完整 Trace（thinking 关闭，temperature=0 真正生效）
uv run mini-dsh run "run the tests and fix the failure" \
  --workspace /path/to/repo --model-profile deepseek \
  --input-usd-per-million 0.15 --output-usd-per-million 0.6 \
  --cached-input-usd-per-million 0.003 \
  --allow-write --allow-shell --allow-command python --trace /tmp/run.jsonl

# 真实模型套件：每个任务重复 3 次（README 上那张表的来源）
uv run mini-dsh benchmark benchmarks/repo/manifest.json \
  --model-profile deepseek --repeat 3 \
  --input-usd-per-million 0.15 --output-usd-per-million 0.6 \
  --cached-input-usd-per-million 0.003 \
  --allow-write --allow-shell --verify-command python \
  --output benchmark-results/repo-real.json

# 离线重渲染报告 + Pareto 分析，不重新调用模型
uv run mini-dsh report benchmark-results/repo-real.json

# 逐步回放 / 对比两条 Trace
uv run mini-dsh trace replay benchmark-results/repo-real-artifacts/traces/repo-cross-file-bug.jsonl
uv run mini-dsh trace diff traces/a.jsonl traces/b.jsonl --left-label full_history --right-label repo_retrieval
```

**报告会把自己的条件写进去。** 费率是 off-peak 的 $0.15 / $0.6 / $0.003 per 1M tokens；换一个时段或换一个模型，同一份报告里就会出现另一组成本数字，所以 `repo-real.json` 的 `model` 块记录 profile、endpoint、thinking 开关、`temperature_effective` 与显式费率，`docs/results/repo-real.md` 的 "Model and rate card" 章节把它们一并呈现。**归档要在重跑之前做**：报告与 Trace 是原地覆盖的，重跑一次就会冲掉上一次的原始产物。

一个真实 provider 如果没有声明价目，会**拒绝运行**而不是报告 `$0`：零价目和未声明价目无法区分，所以"是否已声明"被显式跟踪。本地权重（`ollama`）声明了免费价目因而通过；付费 provider 未声明则报错，`--allow-unpriced` 是显式的逃生口。

**成本模型**按每次 Model Call 计价，区分缓存与未缓存输入：

```text
cost = (billable_input × input_rate + cached_input × cached_rate + output × output_rate) / 1e6
billable_input = input_tokens − cached_input_tokens
```

比总成本更重要的两个派生量是 **cost per successful run**（`总成本 / 成功次数`，便宜但更容易失败的变体不是便宜）和 **cached input share**（决定长上下文策略在生产中是否付得起）。

**延迟模型**拆成三层，因为单一"平均延迟"在真实 provider 下被网络主导、对 Harness 作者毫无信息量：

| 分量 | 来源 | 含义 |
| --- | --- | --- |
| `model_ms` | `model_call_completed.duration_ms` 之和 | 等待 provider，含重试 |
| `tool_ms` | `tool_call_completed.duration_ms` 之和 | 子进程与文件系统 |
| `harness_ms` | `total_ms − model_ms − tool_ms` | Harness 自身开销（残差） |

## 失败分类

单一成功率把两个问题合并了。分类法把终态拆成 9 个类别、再归并成 4 个**层**，完整决策顺序见 [`docs/failure-taxonomy.md`](docs/failure-taxonomy.md)：

| 层 | 类别 | 读法 |
| --- | --- | --- |
| `success` | `none` | 跑完且判据通过 |
| `harness` | `permission_denied` `invalid_tool_call` `tool_error` `budget_exhausted` | **运行时按规格工作**：它正确地拒绝了或限制住了 Agent |
| `agent` | `model_reasoning_failure` `context_miss` `verification_failed` `loop_stuck` | Harness 没问题，模型仍然失败——这才是能力信号 |
| `environment` | `provider_error` | Provider 故障 |

`permission_denied` 是"Harness 正确"的**证据**而不是缺陷。报告只给层分解，不给一个混合的成功率。

## 配置

优先级 `CLI > 环境变量 > minidsh.toml > 默认值`，可打印来源：

```bash
uv run mini-dsh config            # 打印生效值与每个字段的来源
MINIDSH_MAX_STEPS=20 uv run mini-dsh config
```

```toml
# minidsh.toml
[minidsh]
max_steps = 12
max_tokens = 200000
max_seconds = 120
allowed_tools = ["read_file", "search", "write_file"]
denied_tools = ["shell"]
```

## 工程命令

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=minidsh --cov-report=term-missing
```

四项全部为绿：`ruff format --check` 无需重排、`ruff check` 无告警、`mypy --strict` 在 54 个文件上无错误、203 个测试通过（覆盖率 86%，`runtime/` 为 100%）。CI 见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | 仓库级开发约束（12 节，含架构与依赖方向） |
| [`docs/real-model-evaluation.md`](docs/real-model-evaluation.md) | Provider profile、成本与延迟模型、重复采样协议、数字来源说明 |
| [`docs/failure-taxonomy.md`](docs/failure-taxonomy.md) | 失败类别与层归属、决策顺序、Trace 回放/差分工作流 |
| [`docs/benchmark.md`](docs/benchmark.md) | Manifest 格式、指标定义、消融协议与结果解读 |
| [`docs/context-engineering.md`](docs/context-engineering.md) | 六种 Context 策略的实现、成本与选型 |
| [`docs/sandbox-security.md`](docs/sandbox-security.md) | 威胁模型、六类攻击测试与已验证边界 |
| [`docs/technical-highlights.md`](docs/technical-highlights.md) | 技术亮点、5 个设计决策、SWE-bench 路线图 |
| [`docs/adr/`](docs/adr/) | 架构决策记录 |
| [`docs/results/`](docs/results/) | 实验报告与原始指标 |

## 安全边界声明

`local` Process Runner 是**策略层**（工作区、命令、环境、网络），不是容器或内核级隔离。允许执行通用解释器时，它仍可直接发起系统调用或网络访问。不可信模型与不可信仓库必须在外层容器或 VM 中运行并关闭网络；本地模式不宣称为恶意代码提供硬安全边界。详见 [`docs/adr/0002-security-model.md`](docs/adr/0002-security-model.md)。

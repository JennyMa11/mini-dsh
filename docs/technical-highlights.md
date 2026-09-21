# 技术亮点、设计决策与路线图

本文是项目的对外说明材料：第一部分总结技术亮点，第二部分给出 5 个可展开讲的设计决策，第三部分给出通往 SWE-bench 与更复杂 Repo Benchmark 的路线图。

---

## 一、技术亮点

### 1. 把 Harness 当作研究对象，而不是脚手架

大多数 Agent 项目把 Loop、Context、工具、权限揉在一个文件里，只能回答"能不能跑通"。Mini-dsh 的出发点是**"换个设计会不会更好，好多少"**，因此所有机制都被拆成可替换端口，并配一套单变量消融协议。项目能给出的不是"我做了一个 Agent"，而是"我测出了输出上限能把成本降 3.38 倍"。

### 2. 成本是可核算的一等指标

`ModelPricing` 显式给定输入/输出单价，Token 由 Harness 侧确定性测量，报告给出每任务与总成本。所有实验结果都能直接读成"这次改动值不值这个钱"。这也是很多 Agent 项目缺失的部分——它们报准确率，不报账单。

### 3. 可复现性被写进数据契约

每个 Benchmark 任务钉扎 `repo_commit`、`seed`、`model`、`budget`、`isolation` 与成功判据；Runner 复制临时工作区；成功由独立进程退出码判定，从不采信模型自报。任何人 `./scripts/run-all-experiments.sh` 都能得到同一组数字，无需 API Key。

### 4. 默认拒绝的安全模型，且拒绝可观测

路径归属校验（含符号链接）、deny-first 命令策略、网络 `(host, protocol, port)` 白名单、密钥文件拒绝、环境变量 allowlist、进程组超时清理、Trace 二次脱敏——六道防线各配攻击测试。更关键的是，**被拒绝的调用会产出事件**，所以 `permission_denied` / `unsafe_blocked` 是真实计数而非日志字符串。

### 5. 工程门禁全绿

`ruff format --check`、`ruff check`、`mypy --strict`（54 个文件，`src` + `tests`）、`pytest`（203 个测试）四项全绿，CI 覆盖。测试分 `unit` / `integration` / `e2e` 三层，unit 层不碰网络与真实 Shell。`runtime/`（插件依赖校验、生命周期回滚、Service DI、Event Bus）覆盖率为 100%。

### 6. 真实模型实测，而且报告自带实验条件

`repo` 套件在 `deepseek-flash` 上跑 6 个跨文件任务 × 3 次交错重复，**18/18 通过**，每次成功成本 **$0.000937**，86% 输入 Token 命中 prompt cache。比成功率更有信息量的是延迟拆解：**Harness 自身只占墙钟 0.26%**（16.4 ms / 6,410.1 ms），其余全是 provider 往返——"Harness 不是瓶颈"因此是一个测量结果而不是主张。

报告本身记录了 profile、endpoint、thinking 开关、`temperature_effective` 与显式费率，README 上的这些数字由测试与归档报告比对：改一个数字而不同步归档，门禁就会失败。**这些数字是全仓库唯一需要花钱才能产生的数字，因此也是最容易被手改、最需要被门禁盯住的一类。**

---

## 二、5 个可展开讲的设计决策

### 决策 1：端口驱动 + 组合根，而不是全局单例

**问题**：核心 Loop 一旦 import 具体模型 SDK 或 Shell，就再也做不了 Harness 消融。

**做法**：`domain` 只放不可变 dataclass、`enum`、`Protocol`；`application` 只依赖 ports；所有具体实现（模型、Context、工具、Session、Trace、沙箱）在 `composition.py` 一处装配，构造函数注入。

**权衡**：样板代码变多、依赖关系更显式。换来的是 Loop 与测试完全不依赖厂商和操作系统，且"换一个 Context 策略"是改一行构造参数，不是改 Loop。

**可讲的点**：为什么拒绝 Service Locator（依赖不可见、测试隔离差）、为什么首版不做多进程（超出验证 Harness 机制所需）。

### 决策 2：原始 Trace 是事实源，Context 是可再生成的视图

**问题**：摘要与检索都是有损的。如果摘要覆盖原始历史，一次错误压缩就永久污染事实，实验无法审计。

**做法**：`ContextView` 是纯函数输出，带 `strategy` / `version` / `estimated_tokens` / `omitted_messages`；历史只读；检索实现为装饰器而非并列策略。

**权衡**：每个策略要自报账、重复一点元数据代码。换来的是压缩可观测、策略可公平对照、组合不爆炸。

**可讲的点**：这是"数据可审计性"与"实现便利性"之间的取舍，也是我为什么没有直接上向量库（破坏离线确定性与排序可复现）。

### 决策 3：让消融真的有效——从一次失败的实验说起

**问题**：第一版消融跑完，六种 Context 策略的 Token 读数**完全相同**。

**排查**：脚本化响应里写死了 `input_tokens`，所以 Harness 从未真正测量送入模型的上下文——"测量"变成了"复读预置值"。

**做法**：把 `estimate_tokens` 提到 `domain` 层共享；让 `ScriptedModel` 在响应未声明 Token 时**测量实际收到的消息**；重新生成全部 fixture；并专门造 `stress` 套件（18 步长历史、75 KB 输出、含被拒绝动作），因为短任务上差异会被固定开销淹没。同时发现 `permission_denied` 恒为 0——被拒绝的调用没有发布完成事件，于是加了 `_reject` 路径，`stress` 上才出现真实的 `denied=1, blocked=4`。

**追问一层：读数稳定吗？** 修完上述问题后连续跑两次，Token 仍在 11 597 / 11 600 之间抖动。追下去发现 `AgentLoop` 把 `duration_ms`（`28.384750000000002` 这类变长浮点）渲染进了模型可见的反馈——**墙钟时间泄露进 Prompt，Token 计数就永远不可复现**。修法是把 `duration_ms` 留在事件流里（指标仍能读到），不进 Prompt，并加回归测试断言"两次仅时长不同的观测渲染出逐字节相同的消息"。修完后三次运行读数完全一致（`10253 / 16059 / 2290`，成本 $0.095022）。

**权衡 / 收获**：这是整个项目最有价值的一段。它说明**"指标为 0"或"读数抖动"往往不是结论而是 bug**，以及基线必须与默认配置逐字段对齐（后来加了回归测试守住这点）。

### 决策 4：输出有界化——最大的单点成本杠杆

**问题**：一次 `read_file` 命中 75 KB 日志，会把后续每一步的输入成本推高一个数量级。

**做法**：Tool Runtime 统一在产出 Observation 前检查序列化长度（默认 20 000 字符）；超限时把完整原文写入 `.minidsh/artifacts/`，模型只收到 `{preview, original_characters}` 与 `truncated=True`。

**权衡**：截断丢失信息，但**显式、可观测、可回溯**；artifact 不自动回灌 Context，避免重新膨胀。

**实测**：`unbounded_output` 32 136 Token vs `bounded_output` 9 511 Token——3.38 倍成本差，且成功率不变。这说明输出上限是**成本杠杆而非正确性杠杆**。

### 决策 5：默认拒绝 + 拒绝可观测

**问题**：安全策略的常见失败是"看起来拦住了"，但没有任何证据。

**做法**：六道防线（路径归属、符号链接、deny-first 命令、网络三元组白名单、密钥拒绝、进程组清理）各配攻击测试；权限判断发生在执行之前，被拒调用发布 `tool_call_completed` 事件。同时**明确声明边界**：本地 Runner 是策略层不是隔离层，允许执行解释器就无法阻止其内部自发行为，不可信场景必须上 Docker。

**权衡**：诚实标注局限会让项目"看起来没那么安全"。但把边界说清楚，比声称一个不存在的保证更有价值——这也是我在 ADR 0002 里把状态写成 `Accepted with limitations` 的原因。

---

## 三、通往 SWE-bench 与更复杂 Repo Benchmark

`core` / `stress` 是**自建、小规模、脚本化**的，用于验证 Harness 机制。`v0.3.0` 已经把 `repo` 套件（6 个跨文件任务）跑在真实 provider 上，结论是"Harness 侧站得住、任务形态可行"；但 6 个任务 × 3 次重复既小、任务又由 Harness 作者构造，**不构成独立评测**。要证明它在真实任务上有效，必须走向 SWE-bench 这类真实仓库、真实 issue、真实测试的任务集。

### 3.1 SWE-bench 与当前任务的差异

| 维度 | 当前（core / stress） | SWE-bench |
| --- | --- | --- |
| 仓库规模 | 十几个文件 | 数万行、多包 Python 仓库 |
| 任务来源 | 人工构造的 bug/feature | 真实 GitHub issue + 关联 PR |
| 环境 | 纯标准库，`python verify.py` | 每实例独立依赖环境（常需 conda/docker） |
| 判据 | 自写 `verify.py` | 官方 FAIL_TO_PASS / PASS_TO_PASS 测试列表 |
| 步数预算 | 4–18 步 | 数十到上百步 |
| 输入规模 | 单文件为主 | 跨文件、需检索定位 |

结论：**当前架构不需要重写，需要补三层能力——环境构建、补丁提取、官方判据对接。**

### 3.2 路线图

**阶段 A：任务接入（把 SWE-bench 表达成 manifest）**

1. 新增 manifest 生成器：读取 SWE-bench 实例（`instance_id`、`repo`、`base_commit`、`problem_statement`、`test_patch`、`FAIL_TO_PASS`、`PASS_TO_PASS`）。
2. 每个实例映射为一个任务：
   - `repo_commit` = `base_commit`（从 fixture 摘要升级为真实 commit hash）
   - `workspace` = 按 `base_commit` 检出的仓库副本
   - `goal` = `problem_statement`
   - `success_argv` = 应用 `test_patch` 后运行目标测试
3. 环境构建：为每个实例准备依赖（镜像或 conda 环境），`isolation.sandbox_backend` 强制为 `docker`。这一步是 SWE-bench 的主要工程量，**必须先解决环境再谈 Agent 能力**。

**阶段 B：补丁提取与官方判据对接**

1. 运行结束后把工作区 `git diff` 导出为 SWE-bench 预测格式（`instance_id` / `model_patch` / `model_name_or_path`），落 JSONL。
2. 复用官方 `swebench` 评测器（docker-based）作为**外部判据**，避免自实现测试选择逻辑带来的偏差；同时保留内置 Runner 作为快速自检。
3. 关键约束：`test_patch` 必须由评测侧应用，Agent 不得看到或修改测试文件——否则会退化成"改测试骗过判据"。这一点用 `denied_tools` + 只读测试路径强制。

**阶段 C：把 Harness 变量搬到真实规模上**

1. **预算**：把 `max_steps` / `max_tokens` / `max_tool_calls` 提到真实量级（数十步、数十万 Token），观察预算耗尽（`budget_exhausted`）在真实任务上的分布。
2. **Context 策略重跑**：当前结论是"检索在长历史上更贵"，在数万行仓库上需要重新验证——检索质量可能从"净成本"变成"净收益"。`repo_retrieval` 与 `symbol_retrieval` 是主要候选。
3. **输出上限**：真实仓库的测试输出与 diff 更大，`bounded_output` 的收益预期更高，需要重测阈值（20 000 是否合适）。
4. **权限反馈粒度**：当前 `basic_feedback` 更便宜但正确率无法判断；真实模型下应能观测到"反馈粒度 → 重试成功率"的因果。
5. **模型**：从 `ScriptedModel` 切换到真实 provider，接受成功率波动，并引入多 seed 与置信区间。
   → **`v0.2.0` 完成前半、`v0.3.0` 完成真实运行**：Provider Profile、瞬时错误重试、`--repeat` 交错采样、`pass@k` / `repeat_variance` 已就位，`repo` 套件已在 `deepseek-flash` 上跑出 18/18；多 seed 置信区间仍待补。
   → **实测暴露的第一个真实 provider 缺陷**：DeepSeek 上游默认开启 thinking，而 thinking 模式下 API 忽略 `temperature`、且带 `tools` 的请求必须回传历史 `reasoning_content`（否则 400）。这类缺陷在脚本化模型下永远不会出现——"换成真实 provider"本身就是一次对 Harness 的检验，而不只是对模型的检验。

**阶段 D：指标与报告升级**

1. 主指标从 `success_rate` 升级为 **resolve rate**（官方判定），并报告 **cost per resolved instance**——这是最有决策价值的一列。
2. 增加轨迹级指标：首次修改命中率、无效工具调用占比、失败类型分布。
   → **`v0.2.0` 已完成**：失败分类法把 9 个类别归并成 `success` / `harness` / `agent` / `environment` 四层并附证据链；`cost per successful run` 与 `pass@k` 已进入报告；`model / tool / harness` 三层延迟分解已落地。
3. 报告按仓库与难度分层，避免平均值掩盖长尾。

**阶段 E：更复杂的 Repo Benchmark**

> `v0.2.0` 已先行落地一个中间台阶：`benchmarks/repo/` 的 6 个 Repo-Level 任务（跨文件 Bug Fix、定义+调用点契约、测试失败定位、新功能+测试、配置修改、多轮错误恢复）。它们没有 response script、每个跨至少两个模块，是「真实模型能力」结论的最小可信来源；`v0.3.0` 已用真实 provider 把这 6 个任务跑通（18/18，每次成功 $0.000937），并核实 18 条 Trace 里对测试文件的写入为 0。SWE-bench 接入是在此之上把仓库规模与依赖环境提到真实量级，判据换成官方评测器；任务形态本身已经在这里被验证过一遍。

1. **多语言**：`symbol_retrieval` 目前只支持 Python，扩展 Tree-sitter 到 JS/TS/Go/Java/Rust。
2. **长周期任务**：需要跨会话的 Session 恢复与上下文续接（`JsonSessionStore` 已是起点）。
3. **多文件重构**：引入跨文件一致性判据，而不只是单点测试通过。
4. **成本感知策略**：把"预算内最优策略选择"作为一个可研究的 Harness 变量，而不是固定配置。
5. **多 Agent / 并行**：当前明确排除在核心层外；若要引入，应作为端口之上的编排层，保持核心 Loop 单 Agent 的简洁性。

### 3.3 已知风险

- **环境构建是瓶颈**：SWE-bench 大量实例的依赖安装本身就不稳定，可能吃掉大部分工程量。
- **判据偏差**：FAIL_TO_PASS 列表可能包含在 `base_commit` 不存在的测试，必须以官方评测器为准。
- **成本失控**：真实 provider 下单个实例可能花费远超当前实验，需要先跑小规模子集（如 SWE-bench Verified 的 50 例）再全量。
- **安全**：真实仓库是不可信输入，必须全程 Docker + 默认断网，禁止放开 `--allow-network`。

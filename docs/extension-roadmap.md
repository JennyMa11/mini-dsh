# Mini-dsh 扩展升级：执行与验收主计划 v1

日期：2026-10-29。本文是本次升级的范围、交付顺序和验收入口；[AGENTS.md](../AGENTS.md) 为上位约束。

2026-11-04 范围追加：用户明确要求编码工具和 Skills/Verification/Memory 同时启动。
通过独立 `mini-dsh-work` 开发入口复用现有能力，不修改原 M0–M5 协议、默认开关
或历史验收结论；配置、隔离及迁移边界见 [ADR 0027](adr/0027-full-work-startup.md)。
本次仅交付规划，不代表下面功能已经实现或验收。后续开发默认逐项执行本文，不从历史建议推导新需求。

## 1. 目标、范围与完成定义

保留当前 Runtime，在同一套 Plugin 生命周期内补齐统一执行扩展、人工 Skills、Context 组装、有限验证修复和轻量项目 Memory。
最小 Agent 与增强 Coding Agent 使用相同 Loop、Tool Runtime、预算、权限、Session 和 Trace。

固定交付 **M0 基线冻结 + 四个功能 PR + M5 总验收**。PR 1–3 交付可用的扩展编码闭环；PR 4 仅在前三项验收通过后进入。
四个 PR 都属于本计划；Memory 默认关闭，不能因为它列在远期就悄悄省略，也不能提前实施。提前结束需明确记录删减范围。

完成需要同时满足：

1. 所有必测矩阵项有可复核的当前版本证据，关键安全门禁通过，四项工程门禁通过。
2. 两个离线示例可复现：无扩展最小 Agent、开启 Skills/Verification 的 Coding Agent；Memory 有单独的可选配置示例。
3. 同一冻结任务集的对照报告完成，报告效果、消耗、证据覆盖率与未测范围。没有收益也可以交付，不能宣称收益或自动默认启用。
4. 兼容入口、迁移方式、能力开关和限制已写入文档；不存在没有停止条件的修复、评测或自我改进流程。

**本轮不做**：Skill 自动生成/进化、向量检索或向量库、远程 Skill 市场、自动安装插件、跨项目 Memory、Memory 自动批准、Subagent 新能力、Gateway、Cron、分布式执行、GUI/终端新交互、模型训练/微调、新模型厂商、新 Context 策略、大型 Benchmark 扩容。
已有 GUI、协作、MCP、流式、审批、会话分叉保留回归兼容，不删除、不迁入核心、不顺手增强。
不将目录重排、统一命名全仓迁移或新的 `create_agent()` 门面作为交付要求。

## 2. 已核对的现状与实际差距

核对对象：本地工作树；HEAD 为 `a49569d15bcb87d160d2bf9f638e838b410d8179`，存在大量未提交/未跟踪文件。
因此 HEAD **不是**本次规划所依据源码的完整冻结快照。M0 必须保存受控源码清单及 hash，不能重置或覆盖现有工作。

| 能力 | 已有入口 | 本轮差距 / 处理 |
| --- | --- | --- |
| Loop 与显式依赖 | `application/agent_loop.py`、`ports/protocols.py` 的 `AgentDependencies` / `AgentFactory` | 保留；补生命周期及完成决策，不另写 Loop |
| 插件 | `runtime/plugins.py` 的 `PluginManager`、`services.py`、`catalog.py` | 已有依赖检查、启动回滚和清理；统一接入 Hook 服务，不建第二注册体系 |
| Hooks | `runtime/hooks.py` 的 `HookRegistry`，`ExecutionHooks` 端口 | 已有 request/call/observed/finishing；目前排序为 `(priority, owner, sequence)`，不等于注册顺序 |
| Hook 组装 | `RuntimeComponents.hooks`；`configured_runtime.py` 配置路径 | `RuntimeServices` / `plugin_runtime` 缺少同等 Hook 选择入口；两条组装路径需一致 |
| 最小组装 | `composition.py`、`examples/minimal_demo.py`、`examples/custom_runtime.py` | 已存在，不用重建 `composition/` 包；补无扩展与有扩展示例的对照 |
| Context | `context/strategies.py`、`retrieval.py`、`snapshots.py` | 保留 full/sliding/summary/retrieval/retrieval_summary/symbol 六种基础策略及已有 budget 组装分支；增加来源清晰的组装层 |
| 原始事实 | SessionJournal/SessionLog、Trace | 继续追加记录；Context 和 Hook 不覆盖事实 |
| Tool / Permission / Budget | `tools/runtime.py`、`application/agent_loop.py`、infrastructure adapters | 全部复用；扩展发起的读取、验证和记忆操作也必须计账 |
| 完成判断 | Loop 无工具响应分支、`local_policy.py` 的局部保护、`before_finish` 回调 | 尚无通用验证失败后继续运行的完成决策；`before_finish` 返回 `None`，不能作为验证循环 |
| Memory | `benchmark/memory.py` 是进程内存/系统内存压力测量 | 与项目经验记忆无关；Project Memory 属于本轮新增能力 |
| 评测 | `evaluation/harness_metrics.py`、现有 benchmark runners / sidecar | 复用指标与隔离，仅增加本轮冻结协议和必要 case |

规划时定向检查：`test_runtime`、`test_composition`、`test_harness_extensions`、`test_harness_safety`、`test_context_retention` 共 62 项通过。
这是局部回归证据，不是全仓质量门禁，也不是新功能验收。历史真实模型报告继续按原版本引用。
本计划依据本地实现，不依赖草案中未核实的 Pi/Hermes 接口描述。

规划文档检查发现两项既有基线问题：README 声明 41 个测试模块而工作树实际有 42 个；
`docs/resume.md` 与 `docs/technical-highlights.md` 的 mypy 文件数声明不是当前 127 个。
`uv run pytest -q tests/test_docs.py` 得到 14 passed / 2 failed / 2 skipped（部分收集导致跳过）；
本次没有变更源码、测试或这些统计声明。M0 须据实修正并重新执行完整门禁，不能沿用历史绿灯。

## 3. 固定架构与运行契约

```text
CLI / Python embedding
  -> composition.py + PluginManager / ServiceRegistry
     -> application.AgentLoop（只依赖 domain / ports）
        -> Context 组装 -> 现有 Context 策略 -> 模型请求
        -> 完成提议 -> 完成决策 -> Continue 或终态
        -> 受预算约束的调用入口 -> ToolRuntime -> Permission -> adapter
     -> HookRegistry / Skills / Verification / Memory（可选插件能力）
     -> Session 事实 + Event / Trace + 独立验收 sidecar
```

### 3.1 Hooks：补齐一次，能力模块按契约接入

下表为目标契约，具体类型在 PR 1 的契约测试中固定。使用 dataclass/enum/Protocol，禁止 `Any` 万能上下文。
上下文只携带 run/step 身份、只读状态、剩余预算视图和受控能力；不暴露可改计数器、原始文件句柄或裸进程执行器。

| 目标 Hook | 输入 / 允许结果 | 执行与错误规则 |
| --- | --- | --- |
| `on_run_start` | 只读运行信息；初始化扩展的本轮状态 | 每 run 一次；失败则 `failed`，不调用模型 |
| `before_model` | 派生 Context / ModelRequest；返回新视图 | 复用现有 request 路径；不能新增工具能力、改权限或覆盖受保护内容；超限不发模型请求 |
| `before_tool` | ToolCall；允许改写或拒绝 | 保留 call_id，改写后重新查工具、校验 Schema、授权；拒绝形成 Observation |
| `after_tool` | 原始 ToolObservation 的只读副本 | 覆盖成功、失败、拒绝、超时路径；错误事件单独记录，不伪造真实结果、不重试已发生副作用 |
| `on_step_end` | 当前 step 的只读事实 | 每个已开始 step 收束时一次；纯观察失败只记录事件。此处 step 是 Loop 步，不是跨轮对话 turn |
| `before_finish`（新增决策契约） | FinishProposal；返回 `allow` / `continue` / `fail` | 仅候选正常完成触发；`continue` 必须附有界反馈；异常按 `failed` 处理 |
| `on_run_end` | 已确定终态的 RunResult 副本 | 四类终态均触发一次；不能重新开启 Loop 或把失败改成成功；异常记录，不改变已定终态 |

补充规则：

- 不强制扩展实现全部 Hook。旧 `before_request` 和旧 `before_finish(RunResult)->None` 通过兼容 adapter 保留；旧结束回调仍是旧语义，不静默改签名。
- 新能力按显式注册顺序执行；旧 registry 的 priority/owner 排序保留在版本化兼容路径。混用时由组装清单冻结完整顺序，禁止根据字典或插件 ID 意外重排。
- 多个完成检查顺序执行，首个 `continue` / `fail` 即停止本次检查链；后续完成提议重新从头检查。没有检查器默认 `allow`。
- PR 1 引入通用完成决策及 fake checker 契约；PR 3 只增加 Verification 的实现和组装，不再给 Loop 塞入测试命令或 pytest 专用逻辑。
- 观察与策略必须分开。策略失败关闭当前动作；观察回调失败留结构化事件。旧 `after_tool` 失败改变返回状态的行为不能无声沿用到新纯观察接口，须有兼容测试和迁移说明。
- Hook 的等待计入墙钟；每次调用受剩余墙钟限制，清理另有固定短期限（默认 1 秒，仅资源释放，不允许再发模型或工具调用）。同步阻塞/恶意插件不属于可隔离代码，不能宣称 Python Hook 超时等同 OS 隔离。
- 工具 Hook 内禁止递归发起工具。需要 I/O 的扩展阶段通过受控调用端口请求，统一生成 call_id、计账、授权、产出 Observation 和事件。验证器不能持有可绕过计账的裸 ToolExecutor。
- `cancelled`、`budget_exhausted`、`failed` 不经过可继续的完成检查；达到总预算立即停止后续调用。结束通知/资源清理不能启动 Memory 写入。
- Hook Trace 至少记录 schema_version、扩展 ID/版本、phase、run_id/step_id、顺序、耗时、结果/错误及变换摘要；内容脱敏并限长。

### 3.2 Context 与 Skills

组装入口是现有 ContextManager 的组合实现，支持已有 `PreparedContextManager` 的受控检索与计账，不复制六种策略。
有序段：系统约束 → AGENTS 项目约束 → Skills 元数据 → 已选 Skill 正文 → 任务状态/相关 Memory → 策略选中的历史/仓库证据/工具反馈。
最后一段必须保留 assistant/tool 因果配对与先后关系，不能机械拆散后重排。系统约束、当前任务、关键决策、未解决错误、已修改文件始终保留。

每段有来源 ID、版本/hash、信任级别、顺序、Token 估算与省略原因。稳定前缀仅便于重建，不承诺 Provider 缓存命中。
Skill/Memory 是较低信任的任务资料，不覆盖系统/项目约束，不通过伪造 system 消息获得更高权限。
PR 2 为六种基础策略全部加入相同来源契约，已有 budget 分支也必须回归；全局请求上限在 Hook 之后再校验。受保护段放不下时显式失败，不能静默删约束。

Skill v1 的范围固定为：

- 只扫描显式配置的工作区内根目录，固定排序；只支持 UTF-8 `SKILL.md`，frontmatter 仅必填字符串 `name`/`description`，正文为 Markdown，不引入完整 YAML 执行或模板引擎。
- name 使用 snake_case；重名、非法名称、畸形头、无描述、越界符号链接明确拒绝。上限：32 个技能、描述每项 256 字符、单文件 32 KiB、单 run 最多激活 3 个。
- 首次只给元数据；模型通过注册工具 `load_skill(name)` 读取正文。扫描和加载通过受控文件能力，不让 Context 自行读盘；读取占工具与墙钟预算，调用记录标明来源。
- 目录快照/hash 固定到 run；加载前核对来源与权限，文件中途改变则拒绝并要求新 run，不能静默混用版本。
- 正文及资料引用只是知识，不自动执行命令、不自动展开引用、不自动授予网络或写权限。不存在 Skill 选择打分器；模型根据元数据显式选择。
- 人工维护三个小示例：bug_fix、test_debug、code_review。开启 Skills 增加 `load_skill` 是组装期的显式工具注册，不能由请求 Hook 临时塞入。

### 3.3 Verification：有限闭环与独立验收分离

应用配置提供固定 argv、工作目录、超时和可见判据版本，禁止将模型生成的字符串直接当 Shell 命令。
Verification 在完成提议时经受控入口调用已有 test/命令工具。v1 每轮完成提议只执行一个固定检查命令，不做测试自动选择。

```text
完成提议 -> 检查剩余预算 -> 运行可见验证
  通过 -> allow -> finished（仍不等于独立验收成功）
  测试断言失败 -> 记录原始 Observation + 有界反馈 -> continue
  基础设施错误 / 权限拒绝 -> failed + 明确原因
  取消 / 总预算耗尽 -> cancelled / budget_exhausted
```

最多 **2 次反馈修复**，因此最多 3 次完成检查；第三次仍失败则 `failed: verification_failed`。
继续修复沿用同一 run_id、递增 step_id 和原预算，不新建运行重置额度；没有修改的重复完成也消耗该次数。
预算与取消优先于修复限额；在途 Token 超量完整记录，不能再执行“最后一次验证”。
默认不复用旧测试通过结果，最后一次修改后必须重新验证。可见测试通过只记录 `runtime_verification=passed`；独立判据在 Agent 退出后对捕获产物运行，结果单独保存。
现有 local-first 的失败修改保护保留为独立前置限制；Verification 不关闭它、不引入第二套无限拒绝循环。

### 3.4 Memory：受控的小规模项目经验

PR 4 默认关闭。SQLite adapter 只保存项目环境约束、验证过的修复经验和失败定位经验；不保存原始 Prompt、完整工具输出、密钥或隐藏判据。
project_id 由显式项目配置确定，禁止仅靠目录 basename 判断同一项目。记录有 ID、schema_version、project_id、适用路径/错误类型、内容、来源 run_id/证据 hash、适用版本、状态、批准人/时间和修订号。

写入状态机固定为 `proposed -> validated -> approved -> committed`，也允许 `rejected`；每次内容修改清除旧验证/批准。
模型最多提议，不得批准。校验检查来源存在、项目一致、内容限长、敏感项、冲突及证据；经验需要验证证据，环境约束需要操作者确认的配置来源。
批准绑定内容 hash 和修订号；过期批准、不同项目、重复提交、写入取消与 SQLite 事务失败都必须有确定结果。
无独立最终验收的修复可保留 proposed，不自动提升为“已验证经验”。受控批准在运行外显式进行，不靠 on_run_end 自动写盘。

所有读写经 Tool Runtime + Permission；仓库内默认工具不能直接改 Memory 数据文件，受信应用维护入口使用专用能力和显式存储授权，不顺便扩张模型权限。
仅检索 committed、项目一致且适用版本匹配的条目，以路径/错误类型确定性匹配；最多返回 5 条、每条正文 2 KiB、单项目最多 100 条，满额拒绝新增、不自动淘汰。
失效或相互冲突条目不注入并记录原因；本轮只支持人工修订/失效标记，不做自动总结、Embedding、跨项目共享、后台维护或“越用越聪明”承诺。

## 4. 交付顺序与逐阶段门禁

估算以单人、现有代码可复用、无新增研究课题为前提，共约 15–24 个有效开发日；按证据出关，不以日期替代验收。
每次只进行一个阶段；若一个阶段超过估算上界，先记录阻塞与缩减实现方案，不自动增加新功能或换框架。

| 阶段 | 工作包与产物 | 验收 / 出关条件 | 预计 |
| --- | --- | --- | --- |
| M0 基线冻结 | 整理当前源码快照清单；固定开关、离线 case、判据、环境和协议；记录现有失败 | 质量门禁、当前示例、基线 Trace 和关键安全探针可复跑；所有基线失败有归属，阻塞项未清除不能进 PR 1 | 1–2 日 |
| PR 1 扩展契约补齐 | domain/ports 类型；现有 HookRegistry 的版本化兼容；PluginManager 服务接入；生命周期、完成决策、受控计账端口；最小/审计/注入示例 | H01–H08、C01；关闭扩展行为兼容；fake checker 能继续/失败；第三方示例不改 Loop；无 Skills/Memory/真实验证器 | 4–6 日 |
| PR 2 Skills + Context | Skill 解析与受控加载；Context 组合；六策略来源与预算；三个手工示例 | S01–S04、C02–C04；仅元数据渐进加载；事实不可变；安全/配对关系/超限检查通过 | 3–5 日 |
| PR 3 Verification | 基于完成决策的固定命令验证插件；有界反馈与修复计数；增强 Coding 示例 | V01–V05；真实临时工作区中“失败→修复→通过”；取消/预算/拒绝不假成功；隐藏测试独立 | 3–4 日 |
| PR 4 Memory + 对照入口 | SQLite adapter、受控提议/验证/批准/提交入口、确定性检索；复用 benchmark runner 增加冻结开关矩阵 | M01–M04、E01–E03；Memory 默认关闭；运行外批准可复现；证据与评测输入隔离 | 3–5 日 |
| M5 总验收与封版 | 四门禁、完整矩阵、资源探针、冻结模型对照、迁移说明、最终报告 | 所有必测项通过；未测项只允许是明确排除项；收益不足如实报告；关闭任务，不自动开启下一路线 | 1–2 日 |

### 各阶段的文件边界

- PR 1 主要涉及 `domain/`、`ports/`、`runtime/hooks.py`、`runtime/plugins.py`、`runtime/services.py`、`composition.py`、`configured_runtime.py`、Loop 与 Tool Runtime 接入点。不得扩展 CLI 产品交互。
- PR 2 新增模块按现有分层落在 `runtime/skills/`、`adapters/`、`application/context_assembly.py`（名称可在同 PR 小幅调整），复用 `context/`；不移动现有策略目录。
- PR 3 的验证行为在扩展/应用能力实现内，Loop 只消费 PR 1 的通用决策与调用端口。
- PR 4 的存储在 adapter，Memory 规则在 domain/runtime 对应层；`evaluation/harness_metrics.py` 复用，不另造评分器或财务费用体系。
- 所有 PR 都可修改与本阶段直接相关的测试、docs、示例与必要配置字段；越过上述边界必须说明与本阶段验收项的直接关系。

## 5. 验收覆盖矩阵

下列是冻结的最小覆盖集合，**当前全部为“未测（目标契约）”**。已有相近测试只能复用实现，不能自动把新契约标为通过。
每行可以展开参数化子 case，但不能因失败删 case。M0 将每行固定成 manifest，包含任务输入、触发、预期/禁止行为、预算、隔离、判据版本和源码 hash。
证据目录约定 `benchmark-results/extension-v1/<snapshot>/<case_id>/<repeat>/`，每行保存 `check.json`，涉及 Loop 的行还保存 Trace 和绑定 run_id 的 sidecar；安全项另存真实副作用快照。

| ID | 触发及预期行为 / 禁止行为 | 检查方式 | 阶段 |
| --- | --- | --- | --- |
| H01 | 无 Hook 与空 Hook 执行相同脚本；结果、模型输入、工具动作、Token/调用数相同 | 契约 + 规范化 Trace 对比；忽略 ID/时间和新增审计事件 | PR 1 |
| H02 | 插件依赖、重复服务、启动中途失败；逆序释放一次，不残留 Hook | 生命周期故障注入；参考 test_runtime/test_composition | PR 1 |
| H03 | 三个扩展组合、相同优先级及卸载；新顺序固定，旧顺序兼容 | 记录调用序列，重复运行及卸载后复查 | PR 1 |
| H04 | 只修改 Hook 副本；Session、工具能力、call_id 和原始结果不变 | 深层可变数据回归；读取日志逐项比较 | PR 1 |
| H05 | before_tool 改为非法参数/越权路径；不执行；合法改写可执行 | Schema/授权事件 + 外部哨兵文件 hash | PR 1 |
| H06 | 注入各生命周期异常/超时/取消；收束一次，不重试副作用 | 参数化故障测试 + 终态/调用计数；after_tool 全状态覆盖 | PR 1 |
| H07 | fake 完成检查 allow/continue/fail/冲突；先拒绝者生效，原预算不重置 | 脚本模型轨迹；验证两次 step 和唯一终态 | PR 1 |
| H08 | 审计与请求注入扩展走两种组装入口；不改 Loop 即可接入 | 可执行第三方示例；配置/嵌入输出对照 | PR 1 |
| C01 | 无扩展的六种基础 Context 策略及已有 budget 分支仍保留原行为 | 参数化原回归与事实快照 | PR 1 |
| S01 | 启用 Skills 初始只含元数据，load_skill 后才有正文 | 检查每轮实际 ModelRequest、受控读取计账 | PR 2 |
| S02 | 重名/非法名/畸形头/超量/超长/缺失文件 | 固定边界输入，明确拒绝，不静默使用部分目录 | PR 2 |
| S03 | `..`、符号链接逃逸、敏感文件及运行中替换 | 真实临时文件 + 受控后端；无越界读取/泄漏 | PR 2 |
| S04 | Skill 要求执行未授权命令或引用脚本 | 无自动执行；工具显式调用仍被拒；副作用检查 | PR 2 |
| C02 | 各策略走其适用的裁剪/摘要/检索路径；约束与关键事实仍在 | 六策略及 budget 分支参数化保留检查；Session 字节不改 | PR 2 |
| C03 | 来源冲突、重复段、assistant/tool 配对及上下文注入 | 检查角色/顺序/来源、无权限提升、不重复计 Token | PR 2 |
| C04 | 保护段或 Hook 注入超限；检索被拒/取消 | 不发超限请求；读取消耗保留；未知不记零 | PR 2 |
| V01 | 可见验证失败一次，脚本修复后再次完成 | 真实命令 + Trace 检查同 run、递增 step、最终文件 | PR 3 |
| V02 | 连续失败/无修改重复结束；最多 3 次检查 | 统计验证次数与反馈次数；最终 failed | PR 3 |
| V03 | 检查期间取消、超时、权限拒绝或四种总预算耗尽 | 真子进程清理 + 终态/无后续调用；错误分类 | PR 3 |
| V04 | 曾通过后再次修改；必须重验，测试命令来源不能被模型替换 | 固定 argv、修改/验证顺序及旧结果不可复用 | PR 3 |
| V05 | Agent 改坏可见测试或提前退出测试命令 | 可见结论与独立判据分开；隐藏测试/完成标记捕获假成功 | PR 3 |
| M01 | 提议→校验→批准→提交；越级/改内容/旧批准 | 状态机契约 + SQLite 实际读回，批准身份/hash 可追溯 | PR 4 |
| M02 | 跨项目、陈旧、冲突、未批准项和超额数据 | 检索排除与显式拒绝；只返回限定数量及来源 | PR 4 |
| M03 | 取消、事务失败、重复提交、重复打开 | 故障注入 + 重开数据库；原子且幂等，无半条记录 | PR 4 |
| M04 | Secret/隐藏标签/未经验证经验写入和直接文件改写 | 假密钥探针 + 数据库/输出/Trace 检测；模型无批准权限 | PR 4 |
| E01 | 越界写、未授权网络、危险命令及合法对照 | 实际容器/OS 副作用观察；禁止全拦截冒充安全 | M0 / 各 PR / M5 |
| E02 | 工具/验证进程超时和取消、插件收束 | PID/进程组与文件快照；退出后无残留子进程 | M0 / 各 PR / M5 |
| E03 | case 未运行、Trace 中断、未知用量、预算超量 | 独立报告检查分母、null、证据覆盖率与总消耗 | PR 4 / M5 |

检查器必须独立于 Agent 可写工作区；不得用模型自报或 Hook 输出替代真实副作用检查。没有 Docker/平台能力时对应项标“未测/阻塞”，不能用 mock 结果签署 OS 安全验收。

## 6. 可执行的工作流与证据

### 6.1 每个 PR 固定流程

1. 从本计划选定唯一阶段；列出 case ID、涉及文件、兼容路径和本 PR 明确不做项。
2. 先补契约/回归测试并记录失败，再实现；内部低风险文档变动不机械添加测试。
3. 运行定向测试和同一批离线冻结任务，再运行四项全仓门禁。原有失败必须记录，不能归零后声称通过。
4. 跑阶段所需真实工具/隔离探针；保存实际进程、文件、网络与脱敏输出证据。
5. 更新覆盖矩阵记录：预期、实际、pass/fail/未测、命令、commit/源码 hash、证据路径、审核范围。
6. 更新公共文档和迁移示例，写明剩余未测项，才允许进入下一阶段。失败则只修复阻塞该阶段的缺陷，不借机扩范围。

现在即可使用的命令（需已有 uv 环境；不会调用真实模型）：

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python examples/minimal_demo.py
uv run python examples/custom_runtime.py
```

定向回归起点：

```bash
uv run pytest -q tests/test_runtime.py tests/test_composition.py tests/test_harness_extensions.py tests/test_harness_safety.py tests/test_context_retention.py
```

新 case 的自动化入口属于各 PR 产物，不冒充现有命令。PR 4 必须交付 `scripts/extension_acceptance.py`，统一提供 `freeze`、`check`、`run`、`report` 子命令；内部复用现有 runner、隔离与指标。
`freeze` 不访问凭据；`check` 核对输入、源码、判据和隔离；`run` 默认离线，真实模型必须显式指定；`report` 仅读取证据。
CLI 参数与实际示例必须在 PR 4 README 中给出并跑通，M5 不接受只存在于文档的伪命令。
在 PR 4 之前由现有 pytest 和逐 PR 冻结任务配置产生证据，不为每个阶段再建一个 runner。

### 6.2 冻结包与证据内容

- `manifest.json`：plan_version、case 清单、预期/禁止行为、判据版本、预算、隔离、重复次数、排序和输入 hash。
- `source_manifest.json`：参与运行的源码、测试、锁文件、配置的 hash；仅按明确 allowlist 收集，禁止归档 `.env`、凭据、主目录和未筛选的全仓数据。
- 每次运行：原始 `trace.jsonl`、实际生效配置、终态、用量、产物 hash、独立 `evidence.json` 和 `check.json`；禁止覆写旧目录。
- 汇总：`coverage.json`、`report.json`、简短 `report.md`，包含评分器版本/hash、所有未运行行、失败归属与未测范围。
- 纯组件 case 没有 run_id 时使用 test ID 与源码 hash；不能伪造 Loop Trace。端到端/轨迹证据必须绑定实际 run_id。
- 报告补算写入新目录，并标记 derived；历史 Trace 不能补认当前版本新能力。

## 7. 对照实验协议：验证功能与评估收益分别做

每个 PR 跑相同离线回归与阶段 case；**不要求每个 PR 都烧一轮真实模型预算**。真实模型配对在 M5 对同一最终源码、同一组开关运行一次完整冻结队列。
新的 Context 组装基础设施在所有组中相同；关闭 Skills/Memory 时恒等输出。PR 1/2 用离线比较证明其关闭路径兼容，不把源码版本差异混进功能收益。

| 组 | Hook 路径 | Skills | Verification | Memory | 可比较的唯一变量 |
| --- | --- | --- | --- | --- | --- |
| Baseline | 关闭可选扩展 | 关 | 关 | 关 | 与 A 比基础设施 |
| A | 空实现/纯审计，不改变模型输入 | 关 | 关 | 关 | A − Baseline |
| B | 同 A | 开 | 关 | 关 | B − A：Skills 包含其显式加载工具 |
| C | 同 A | 开 | 开 | 关 | C − B：固定可见验证 |
| D | 同 A | 开 | 开 | 开 | D − C：已批准项目记忆 |

这只能估计“前序能力已经开启时”的增量效果；D − Baseline 是组合效果，不能归因到某一个模块。不开全因子矩阵，不因结果不显著自动加组。

冻结队列：6 个任务 × 3 次重复 × 5 组 = **90 次**。
其中四题复用 `benchmarks/public_lite/v1/selection.json` 的 Python/0、Python/1、JavaScript/26、JavaScript/151；两题为新增的固定小型项目夹具：一个测试失败修复、一个依赖项目约束的修复。
M0 固定两题的具体输入、可见测试与隐藏判据；参考解全过、缺陷实现失败才可冻结。它们同时用于各 PR 离线回归，不临时挑容易题。
公开题属于已知公开任务，只能作为回归与有限可用性证据，不能宣称未污染泛化性能。

资源固定：单 Agent 顺序、每题 16 步/32 工具调用/180 秒/8,000 Token，模型最大输出 1,024 Token；名义总预算 **720,000 Token**。
模型使用现有 DeepSeek adapter；M0 冻结具体 model ID、响应版本标识（若提供）、temperature=0、thinking 关闭、请求参数和 endpoint 身份，不冻结或归档密钥。
测试命令上限 30 秒，独立判据 10 秒；容器原生架构、1 CPU/1 GiB/禁网，镜像 digest 在准备阶段冻结。
模型 API 网络与 Agent 工具网络分开：前者显式许可 endpoint，后者保持禁网。

组顺序按固定种子 `20261029` 在 case/repeat 内预先打乱，完整顺序写入 manifest；每次独立工作区、Session、容器和 Memory 副本。
Memory 种子只来自另外两个固定教学夹具与人工确认配置，先验证并批准、冻结 hash；禁止从这 6 个评测题的答案、隐藏测试、既往运行反馈生成种子。
同一批评测中不积累 Memory；后运行组不能继承前运行组修复经验。报告 Memory 匹配的机会数，不把未匹配题当成已验证记忆效果。

用量未知立即停止模型队列并审计；达到单题/整轮预算停止后续调用。实际超量完整保留，未运行仍占 90 行声明机会；不得挑失败题单独重跑后替换成绩。
一次完整队列是本轮上限。若实现缺陷导致队列无效，先封存结果，后续重跑须变更记录明确版本、原因和新的总预算；不能后台自动重试。

### 判定阈值（运行前固定）

- 正确性：所有确定性契约和安全必测项通过；Baseline/A 的模型请求、动作、调用数、Token 与结果在离线脚本任务中完全一致（仅规范化身份/时间/审计事件）。
- 兼容性：冻结离线任务无新增失败；现有公开入口回归全过。随机模型不要求逐字相同，不用单次差异替代兼容检查。
- Hook 开销：离线内存模型/工具，预热 20 次、测量 200 次、做 3 批；每批比较总耗时 P95，再取三批差值中位数。A 相对 Baseline 增量不超过 `max(5 ms, Baseline P95 的 10%)`；比较机器和负载记录在案，不将宿主抖动归因模型。
- 收益报告：按 case 聚类（每 case 含三次重复）的配对 bootstrap，固定种子、10,000 次重采样、95% percentile 区间；成功率、Token/成功、延迟都报告差值、样本数与区间。6 个任务的区间仅探索性，不能宣传总体收益。
- 正向收益的最低口径：成功率差值区间下界 > 0，或在成功率点估计不下降的前提下 Token/成功差值区间上界 < 0；后一项只称效率迹象，不宣称成功率非劣。两者都须安全门禁通过且用量完整；无成功样本时 Token/成功为 null。
- 不满足上述收益口径：记录“未观察到明确收益”，功能仍可作为可选能力交付，Skills/Verification/Memory 不据此默认开启，不扩任务集追求显著性。

指标沿用 [Harness 评测](harness-evaluation.md)：任务成功与约束内成功分开、分子/已检查分母/声明机会/覆盖率齐全；Token 包含失败用量，不计算货币费用。
恢复指标只在预先声明的故障事件与检查期限下启用；模型自报、Schema 有效、随后一次调用成功都不能替代恢复证据。

## 8. 范围控制、风险与停止条件

| 风险 | 固定应对 | 不允许的绕行 |
| --- | --- | --- |
| 当前工作树未冻结 | M0 记录源码清单和既有改动 | 用 HEAD 冒充当前版本、丢弃用户修改 |
| Hook 与旧 API 语义冲突 | 版本化 adapter、明确新旧顺序与错误规则 | 静默改行为、创建另一注册系统 |
| 验证需要再进 Loop | PR 1 建通用完成决策、PR 3 只实现能力 | 在 on_run_end 递归调用新 run 重置预算 |
| Context 太大 / 记忆污染 | 固定限额、来源与失败策略 | 自动放宽预算、自动削掉约束或自动批准 |
| 某机制没有收益 | 保留可选能力并如实收尾 | 加功能/换题/重跑到“有效”为止 |
| 安全能力在当前平台不可验证 | 阻塞相应出关，记录所缺环境 | 放宽宿主安全边界、用模拟通过替代 |

新增请求先判定：是否为通过本计划某个 case 的必要修复？是，则绑定该 case；否则进入“延期项”，本轮不实现。
任何范围变化需在本文末尾记录变更 ID、原因、原/新范围、被替代项、验收变化、资源变化和用户明确决策。涉及依赖方向、信任或数据所有权还需 ADR。
新需求不能通过“顺手优化”“以后可能要用”“参考产品已有”自动加入。未经明确变更，后续 Agent 只执行当前阶段。

每个 PR 最多两轮集中修复与复验；仍未满足门禁时提交具体阻塞说明和最小修复范围，保持阶段未完成，不无限扩写。此规则不允许隐藏失败或跳过验收。
M5 结论按“功能通过/失败/未测、机制收益、剩余排除项”分别写明；满足完成定义即封版，下一轮必须有新任务范围。

## 9. 第一条开发任务与交付模板

后续可直接下达：

> 依据 docs/extension-roadmap.md v1，先完成 M0 基线冻结，再实施 PR 1。复用现有 PluginManager、ServiceRegistry、HookRegistry、ExecutionHooks、AgentFactory 和两条组装路径。先写 H01–H08/C01 的契约测试，再补齐生命周期、顺序兼容、受控计账及完成决策。只用 fake checker 验证 Continue/Finish，不实现 Skills、Memory 或真实验证扩展。不覆盖已有工作树改动；输出源码快照身份、修改范围、门禁结果、证据路径和未测项。PR 1 验收通过后停止。

每次交付固定写明：阶段/源码身份；本阶段 case 与实际行为；四门禁结果；真实副作用检查；证据路径；已知限制；是否允许进入下一阶段。
当前执行进度（2026-10-29）：M0 已验收，见 [M0 报告](results/extension-m0-2026-10-29.md)。PR 1 的接口与迁移见 [Hook 契约](extension-hooks.md)，出关状态以 [阶段验收报告](results/extension-pr1-2026-10-29.md) 为准；目标恢复后进入 PR 2，接口与用法见 [Skills 开发记录](extension-skills.md)，阶段出关状态以 [PR 2 独立报告](results/extension-pr2-2026-10-29.md) 为准；PR 3 接口与用法见 [Verification 开发记录](extension-verification.md)，出关状态以 [PR 3 独立报告](results/extension-pr3-2026-10-29.md) 为准；PR 4 已进入组件开发，见 [Memory 开发记录](extension-memory.md)；M5 尚未开始。原始 v1 规划时状态为 M0 未完成、所有功能阶段未开始。

变更记录：v1，2026-10-29，依据当前工作树建立初始范围；未开展功能实现或真实模型实验。

### 2026-11-05：配置与模板资料补充（T01）

- 原因/用户决策：用户在比较七类提示词职责后要求“根据上述知识，补全缺少的配置”。本次按可审阅模板和现有配置示例落地，未推定授权自动技能进化或后台维护。
- 原/新范围：原计划未提供独立模板资料包；新增 `examples/prompt_templates/` 七类人工模板、说明和可由现有解析器读取的 runtime.toml。属于单独的配置/文档补充阶段，不替代 PR 4 或启动 M5。
- 被替代项：无。运行默认值、Context 算法、Memory 状态机、Skill 快照和原冻结协议不变。不添加新配置键或模板加载器。
- 验收变化：检查示例默认及命名 profile 均可解析，核对文档链接和四项工程门禁。模板内容是人工审阅规范；语义摘要恢复、自动复盘、缓存收益均未测，不能据此补认 C02–C04/M01–M04。
- 资源变化：无新增模型调用、真实模型评测、后台任务或外部副作用。若后续要求自动执行这些模板，再单独记录范围、ADR、预算与验收。

2026-11-05 用户后续明确决定：自动提炼 Skill 功能先关闭。当前未实现自动提炼入口，继续保持不启用，不接入后台 Skill/Curator Review 或自动写入/修订；仅保留人工模板和样例。既有人工 Skills 的发现与显式加载不变，重新启用须有新的用户明确要求。

## 2026-11-06 范围追加：Trainable Coding Agent 研究规划

用户明确请求下一阶段 Agentic RL 实验方案。本次仅新增[实验规划](trainable-agent-rl/experiment-plan.md)、[实施门禁](trainable-agent-rl/implementation-roadmap.md)及[预算](trainable-agent-rl/cost-estimation.md)，拟采用独立实验包复用当前Runtime；边界与迁移见[ADR 0034](adr/0034-trainable-agent-experiments.md)（Proposed）。
原M0–M5范围和历史结论不变；本追加不启动训练、不实现RL模块。未来先执行Smoke，验收后再逐阶段推进，不能由此自动扩充SFT、外部大型任务集或其他消融。

2026-11-06 后续范围收缩：用户要求预算降至¥50–100，当前执行范围改为[轻量实验](trainable-agent-rl/lightweight-experiment.md)：¥50核心版，必要时累计¥100增加一个Reward对照臂；原大规模四阶段与¥1380预算仅作未来参考。本次仍只修订规划，不启动实现或训练。

2026-11-06 核心版实施授权：用户设定目标“完成50¥的核心版实验”。开始轻量版S0离线契约、任务判据与只读服务预检；累计付费上限¥50，按当前阶段门禁推进，不启用¥100奖励消融或正式大规模队列。实验依赖隔离在 `experiments/trainable_agent/`，原AgentLoop不改。

2026-11-06 S0范围变更：第一次8条工程校准均因Qwen3.5原生XML工具协议和中途system反馈渲染不兼容失败，完整保留在calibration-01；这是adapter故障，不作为模型工具能力不足的证据。修复并加回归后，在同一¥50全局账本内增加8条版本v2校准复跑；S0原16条队列因此加上这8条失败尝试，共最多24条，不增加主训练/评测分母或费用上限。不替换原失败行，不启动¥100版。新增开销从既有¥10 Smoke额度支付。

2026-11-06 第二次S0校准：calibration-02的8条轨迹均耗尽步骤预算，真实Trace显示模型使用绝对cwd被拒绝、重复读取及不适用的编辑命令。训练/validation/test尚未开始。将固定系统指引补足允许程序、相对cwd、python3 argv编辑示例和验证命令；仅以工程夹具做最后一轮8条calibration-03，保留前16条失败。S0因此最多32条（24校准+8训练Smoke），核心总队列最多192条，累计¥50和Smoke¥10不变。此变更不使用测试结果选配置，不隐去失败，也不放宽工具权限或隔离。

2026-11-06 S0训练数值调查：后端optim_step不返回grad_norm且拒绝零学习率；改用独立LoRA导出权重差值验收，前述失败与费用保留。实测后端采用sample mean，故按N_datum/N_assistant补偿到assistant token mean。全零mask权重差为0。Train恢复后同一步更新差异为更新量的2.7378%，通过运行前声明的3%数值门槛，非bitwise一致。

同一sampler重算Logprob仍按1e-3门槛验收；跨采样/训练引擎的1e-3预期未满足，已另存precision-investigation。35个独立生成位置的平均绝对差0.000711、最大0.015747，PPO p/q在0.98438–1.00608、无越clip，独立PPO loss计算误差≤1.4e-6；保留“未达跨引擎bitwise精度”限制。后续使用真实采样q与训练p做重要性比值，不把p重算当旧q；每批记录比值与clip比例，超过5%动作Token越clip则停止优化调查。此门禁变更只依据工程探针，在主训练/validation/test之前冻结，不使用最终成功率调整阈值。

2026-11-06 核心训练阶段完成：固定20槽×4=80条、8次非零更新、12组零信号跳过，全部组的Reward/Token/Datum hash审计通过。训练前已冻结family分割、源码、判据及Base/RL共用Harness；开始固定80条配对验证/测试，不增加Reward臂、不按验证或测试结果选checkpoint。预算累计上限仍50元，所有S0失败和准备调用保留在同一账本；最终效果与费用见独立结果报告，训练集60/80通过不作为泛化收益证据。

2026-11-06 核心版收尾：全部192条工程/训练/评测尝试完成，Test Base 25/32、RL 24/32，未观察到成功率收益；保守费用31.38元、无pending。正式事实与限制见[结果报告](results/trainable-agent-core-2026-11-06.md)及派生JSON。停止付费运行，不启动100元奖励臂；ADR0034接受独立实验包边界，不将本次探索结果视为正式机制收益或既有阶段补验。

2026-11-06 初步结果后的扩展规划：用户要求根据实测效果与保守31.38/已入账3.37的差异重新规划。新增[扩展方案](trainable-agent-rl/expansion-plan.md)：先校准费用控制与新任务，40槽Pilot后有条件延长至120槽，再考虑独立seed或成本Reward。建议累计50/100/150元分档，保持原核心结果与账本；本次仅文档规划，不授权自动扩额、改预算账本或启动新训练。实际实施仍逐阶段验收，并以新test与固定协议隔离开发反馈。

2026-11-06 用户设定“继续实验”目标，授权按扩展方案开始A阶段。当前先沿用累计50元上限，实施精细费用契约、只读预检及原始拒绝轨迹诊断，事实见[扩展执行记录](trainable-agent-rl/expansion-execution.md)。尚未进入P，不因规划中100/150元档自动提额；旧核心实验保持冻结。

### Trainable Agent：¥50 内终点评测顺序调整（2026-11-07）

独立实验范围按[累计50元终点评测调整](trainable-agent-rl/pilot-budget-amendment.md)记录：在Pilot validation及新test采样之前，冻结40槽最终checkpoint，不按val方向决定是否测试；优先预留完整128条新test配对评测，条件为全部训练/验证及数值/费用门禁通过且剩余额度≥7.68元（依据硬Token限额与逐RPC取整证明，v2替代原10.24元余量）。原60/120槽扩展推迟，不扩大累计上限；不改变核心Harness范围及既有验收结论。

### Trainable Agent：Lab GSPO 与 SwanLab（2026-11-07）

用户授权参考 Lab 迁移适合的实验并上传数据。按 [ADR0035](adr/0035-lab-gspo-swanlab.md) 在独立实验包增加多轮 GSPO、自定义 Loss 数值探针与 SwanLab 指标观察；本阶段仅两个工程组 Smoke（8条）及数值验证，新增费用上限5元、累计仍50元。历史 GRPO 测试只做标记明确的指标导入，不用于选择新算法；不扩展核心 Harness 或既有阶段验收。

2026-11-07 GSPO迁移工程阶段关闭：[结果与证据](trainable-agent-rl/lab-migration-results.md)。8条轨迹/2次更新、远端CE sum缩放及Mask检查通过；历史和新Smoke指标均进入SwanLab私有项目。新增保守费用3.036元（含首轮数值失败0.456元），累计44.199元，无pending；没有形成GSPO优于GRPO的结论，不自动进入后续Pilot。

### 小仓库交互修复（2026-11-07）

用户授权按新规划继续。当前阶段按[ADR0036](adr/0036-minirepo-interaction-experiment.md)制作8个开发任务和受控工具，通过离线任务与真实Docker安全门禁后，进行32条Base一次提交/多轮修复对照。累计仍50元；训练、正式test和新Reward须按开发结果及费用逐阶段准入，不改变旧阶段验收。

小仓库阶段按预声明门禁结束：64条完整开发诊断，两档配置single/interactive均1/16，机制门禁与资金准入均未通过。[结果与局限](trainable-agent-rl/minirepo-results.md)。累计保守估算48.155元，无pending；不开始RL、第三批或正式test，不补认其他阶段能力。

### 交互可学习性优先（2026-11-07）

用户新目标为“先解决交互可学习性，再继续比较RL算法”。按[ADR0037](adr/0037-interaction-learnability.md)
在独立实验包接入固定工具示例，进行确定性反馈修复、原Prompt一致性、精确tokenizer和完整
配对队列准备。当前阶段不改变工具、安全边界、任务或资源额度；保留累计50元，资金不足时
在服务连接前拒绝。已实现与待验证项见[执行记录](trainable-agent-rl/interaction-learnability-execution.md)。
路径正确不代表模型可学习性已解决；新训练与算法比较须后续实测和独立资金准入。

2026-11-07 当前交互阶段补齐[已结算小时覆盖审计](trainable-agent-rl/closed-hour-reconciliation.md)：
只将最近两批741次采样的3.956元估价，替换为对应两个整工作空间小时费用加显示余量1.710元。
保留全部操作ID、历史及2元不确定预留，累计保守45.909元、可用4.091元，仍为50元上限。
完整32条工具示例配对预留2.88元可准入；不得以此自动启动训练或额外开发臂。

2026-11-07 工具示例对照结束：[结果](trainable-agent-rl/interaction-learnability-results.md)。
3/16对4/16，反馈后修复门禁未通过；累计保守48.021元、剩余1.979元。
当前唯一阶段改为[ADR0038](adr/0038-batch-read-learnability.md)的可选批量读取离线开发：
在独立实验包增加read_files、真实Docker内容/路径/共享上限检查、脚本反馈控制与精确渲染，
冻结[H2完整32条协议](trainable-agent-rl/batch-read-plan.md)。模型、原Prompt、任务和额度不变；
H2前瞻性检查任意第k次失败候选后再修改，不改变旧H1口径。当前资金不足，不启动付费
队列、训练或算法比较；后续必须先有可审计费用与完整队列准入，不自动提高50元上限。

2026-11-07 H2离线门禁通过，随后完成[四小时覆盖审计](trainable-agent-rl/pilot-hour-reconciliation.md)：
2,282项旧采样/训练操作的15.822元估价，由整工作空间闭合账期加显示余量9.720元覆盖，
保留管理、历史、2元不确定预留及全部操作ID；保守累计41.919元、可用8.081元，无pending。
H2完整32条现具备2.88元资金容量；首次未准入目录保留，当前仍无新模型采样或训练。
当前只允许按冻结H2协议及实时源码/报价门禁继续，不自动加入RL或算法比较。

2026-11-07 H2完整32条结束：[结果](trainable-agent-rl/batch-read-results.md)。两组均3/16，
处理组7/16成功使用批量读取；两组各1条反馈后修复成功，均为billing家族，门槛未过。
新增保守估价2.094元、累计44.013元、可用5.987元，无pending、无训练更新。当前仅基于
超项参数拒绝等证据审查下一项单变量假设，不自动修改限制、补采样或进入RL算法比较。

2026-11-07 下一唯一阶段按[ADR0039](adr/0039-batch-path-limit.md)进入H3离线准备：
原H2的9个超项请求原型重放中，8项上限接受7个但其中6个截断，2个仍路径拒绝。
选择只比较4/8路径数，保留5000字符总输出、全部权限与资源限制；不同时改错误反馈。
实施范围为独立实验工具/driver、模式selector、评分和[完整32条协议](trainable-agent-rl/batch-limit-plan.md)。
离线契约、精确渲染、任务/source门禁和完整2.88元资金均通过后才可执行同期对照；累计
仍50元，当前保守44.013元、可用5.987元，不自动进入RL、另加消融或扩大任务集。

2026-11-07 H3完整32条结束：[结果](trainable-agent-rl/batch-limit-results.md)。两组均4/16，
八项组消除超项拒绝，4个截断项均在修改前完整补读，并在inventory/windows形成反馈修复；
成功率差0，综合门槛仍失败。新增估价2.122元、累计46.135元、可用3.865元、无pending。
当前只离线收敛3对收益/3对退化与后续数据/预算，不开启RL或第四轮工具校准，也不修改
旧门槛、默认四项配置或安全边界；目标保持进行中。

2026-11-07 当前唯一阶段进入[ADR0040](adr/0040-feedback-coverage-audit.md)离线反馈覆盖审计：
固定原8题及原判据，对reference、原bug、常数错误解、逐原始缺陷及公开规格语义探针
分别执行公开/私有验证，保留分母、拒绝/超时与证据hash。只新增独立审计模块和契约测试，
不修改旧任务、结果、Reward、Loop或工具，不启动付费模型/训练，累计50元上限不变。

离线审计初步证据：16/16原始缺陷被公开反馈检出，8/8语义实例（4种唯一探针）未被
公开反馈检出而被私有判据拒绝。当前阶段补充显式新ID的development反馈草稿v2及同探针
再验收，只改新对象的public_tests.py，不改原任务或私有checker；不视为新家族训练/test。

2026-11-07 [离线反馈审计结果](trainable-agent-rl/feedback-coverage-results.md)：原版及新ID草稿
各48候选/96次检查完整完成，reference均通过；定向语义探针从公开漏检8/8变为检出8/8，
私有checker不变。此为4种唯一语义程序的有限覆盖修复，不能补认H3模型门槛或泛化收益。
当前无新增模型/训练消耗，保守累计46.135元、可用3.865元；下一步只做可审计费用及新家族
训练协议准备，完整采样/训练/评测未证明可容纳前不运行付费队列，不自动开始RL算法比较。

2026-11-07 当前阶段按[ADR0041](adr/0041-interaction-learning-readiness.md)完成离线协议准备。
[H1–H3闭合小时审计](trainable-agent-rl/feedback-hour-reconciliation.md)核对1125次采样，
用整工作空间三小时费用加显示余量2.86元覆盖原6.328元估价；保守占用42.667元、可用
7.333元，无pending、累计仍50元，历史、管理及2元不确定预留不变。
[下一阶段草案](trainable-agent-rl/interaction-learning-readiness.md)提出8个新候选家族、
16题及完整80条readiness/训练/评测计划；新夹具、训练桥接和模型收益均未实现/验证。
离线预算上界19.702元超过可用额度，model_queue_admitted=false；不启动新采样/训练，
不提额、不比较算法，也不补认旧H1–H3门槛。下一步先完成新数据与桥接的阶段范围及
离线验收，再按具体完整协议决定资金准入。

2026-11-07 当前唯一阶段按[ADR0042](adr/0042-learning-fixtures.md)实施离线新家族夹具：
train/test各4家族、共16题，每题2个跨模块缺陷及2个额外语义探针；先写契约，再执行
独立public/private容器检查及去重分析。只新增实验层LearningCase、夹具和离线审计，
旧数据、Loop、权限和账本不变；训练桥接待数据门禁之后，不启动付费模型或训练。

2026-11-07 [新夹具阶段结果](trainable-agent-rl/learning-fixtures-results.md)：16题/112探针，
224次真实public/private容器检查完整结束，reference全通过、已声明错误候选均功能性
失败，无拒绝/超时。静态审查将router/media合并为match_rank且同置train，record_diff
移入test；dataset_v2有3个train算法组/4个test组，公开文件、候选和checker不变，
旧检查按hash派生绑定，未重新实测。覆盖只限有限探针，不承认模型可学习性或泛化收益。
当前仍42.667/50元、可用7.333元；下一步准备typed训练桥接，尚未进入模型/训练阶段。

2026-11-07 当前唯一阶段按[ADR0043](adr/0043-learning-trajectory-bridge.md)实施离线桥接：
新增独立LearningCase/轨迹版本绑定和采样进入/返回记录，验证原始Token、policy/seed、
Trace及独立Reward一致后组装G4。脚本轨迹明确隔离，未知采样及不一致拒绝；失败保留。
只增加契约与固定脚本真实容器控制，不修改旧记录、Loop、权限、Reward或费用，
不创建真实采样/训练客户端、不准入模型readiness或算法比较。

2026-11-07 [桥接阶段结果](trainable-agent-rl/learning-bridge-results.md)：新typed读取器、
采样边界receipt及G4组装已实现，四项固定脚本真实容器控制通过；均明确scripted_control，
禁止进入provider训练。绑定schema2显式记录数据/判据版本及policy/tokenizer；预算超量
保留结果和消耗但拒绝更新。原Loop、旧记录和财务账本不变，无provider或训练调用。
当前仍42.667/50元；新任务完整调度、真实provider身份/数值与恢复未验收，下一阶段先
离线准备执行和完整准入，不能从桥接通过推定学习收益或启动算法比较。

2026-11-07 当前唯一阶段按[ADR0044](adr/0044-learning-orchestration.md)实施离线调度：
冻结80条队列，单写者状态、完整资金检查、确认receipt后的恢复及未知更新拒绝重放。
每个有效组保存optimizer state并复用最后sampler，完整保守分配修订为20.302元；
活动上限50元及7.333元可用不变，不启动SDK、模型readiness、训练或算法比较。
本阶段仅用显式contract fixture检查控制契约，真实SDK身份/数值/恢复仍须独立验收。

2026-11-07 [离线调度验收](trainable-agent-rl/learning-orchestration-results.md)完成定向15项
检查：固定80行、readiness拒绝保留64行、确认回执恢复不重放、未知optimizer拒绝、
全同Reward跳过且checkpoint不变、单写者及输入/源码库存变更拒绝。均为contract fixture，
不替代真实采样、SDK恢复或模型收益。只读活动账本准入报告固定缺口12.969元，实际
factory调用0、账本hash不变；具体provider适配与数值门禁仍待后续阶段，不启动付费队列。

2026-11-07 下一唯一阶段按[ADR0045](adr/0045-learning-provider-adapter.md)接入具体SDK
适配与可审计反馈复评。显式注入service/持锁账本，复用现有训练器和采样器，分别保存
Train与Sampler回执，恢复包含optimizer。仅假客户端与脚本容器验收，不连接真实服务、
不启动数值Smoke/readiness/训练，不提额或更改旧实验门槛。

2026-11-07 [SDK适配离线结果](trainable-agent-rl/learning-provider-results.md)：具体RuntimePort
已接入PricedBackend/TrainerV2和封存轨迹，Train/Sampler分别保存、恢复重连initial/current；
19项假SDK/脚本检查验证未知操作停止、零信号、身份、反馈顺序和完整本地证据复用。
全部为contract fixture或scripted_control，无真实模型/训练结果；真实执行入口、身份及
数值门禁仍未验收。活动上限50元、42.667元占用及7.333元可用不变。

2026-11-07 下一唯一阶段按[ADR0046](adr/0046-learning-execution-admission.md)实施固定
配置和真实执行入口。离线复核未变的数值核心及历史权重，明确旧组件证据/新实测边界；
准备初始LoRA结构检查和执行前资金/报价门禁。本阶段不实际run、不连接模型/训练或提额。

2026-11-07 [固定执行入口结果](trainable-agent-rl/learning-execution-results.md)：prepare/inspect
离线可用，显式run具完整资金及配置/SDK/组件证据门禁；旧Mask和Adam恢复按实际旧
权重重算，明确historical_component_reuse。新增initial LoRA结构门禁及准备费用一次
记账，33项定向/257项实验检查通过。真实初始导出和学习收益仍未测；持锁入口在资金
不足时preflight/factory均0调用，账本不变。下一步优先复核已结算费用覆盖，不降低旧
门槛、不缩小目标或自动提高50元上限。

2026-11-07 下一唯一阶段按[ADR0047](adr/0047-closed-period-budget-coverage.md)进行整账期
覆盖审计。刷新现有账单，核对自有历史资源、原始请求及全部operation ID；如证据充分，
以新epoch承接完整已覆盖上界及全部保留估价，不覆写父账本、丢ID或提高50元上限。
证据不足只记候选，不释放预留；即使资金通过也不自动启动模型/训练。

2026-11-07 [整账期覆盖预审结果](trainable-agent-rl/closed-period-budget-coverage.md)：
刷新后选定工作空间账单19.01元，7个历史训练资源归属一致，第二条整数算法复算通过。
全部6971个ID的请求/类别/账期覆盖尚未完成，条件占用29.211元仅作候选；活动账本
42.667元不变，未建立新epoch、未使用重置卡、未启动模型/训练。后续仍限补足覆盖
证据和独立复核，不能把本次算术检查当成资金或科学准入。

2026-11-07 [旧Core分母核查及分阶段资金评估](trainable-agent-rl/staged-funding-feasibility.md)：
1257次原始Trace调用加1次SDK采样匹配旧1258个UUID数量，但排序账本没有逐调用关联；
旧采样25.16元、其他残差6.22元均不能自动认作结算。活动账本仍42.667元。
完整80行不变时，Readiness2.15元、单训练组2.409元、完整test3.38元各自可放入当前
余额，但完整顺序仍依赖实际结算。仅完成可行性评估，未实现新资源模式或运行实验。
若选择该路径，下一阶段先记录ADR并验证固定阶段准入、可恢复资金等待、有限恢复
及新请求账单关联；旧prepared-01保持不变，禁止通过删整轮门禁临时放行。

2026-11-07 下一唯一阶段按[ADR0048](adr/0048-confirmed-stage-funding.md)实施显式冻结的
confirmed_stage_v1资源策略。保留默认整轮模式和固定80行科学队列，先契约测试再实现
完整阶段资金准入、边界等待与有界恢复；重新prepare，不覆写旧计划、不自动结算或
启动真实采样/训练。原50元上限、20.302元整轮分配和全部科学门禁保持不变。

2026-11-07 [确认阶段资金门禁结果](trainable-agent-rl/staged-funding-results.md)：显式
confirmed_stage_v1与原模式共享80行及20.302元完整分配，支持边界资金等待和最多12次
有界恢复；无占用下降的重复等待不创建SDK或消耗恢复次数。新旧科学字段逐行一致，
203个源码归档成员复核通过，1072/273项完整回归通过。离线inspect显示Readiness
2.15元可容纳，但整轮20.302元仍不足，execution_admitted=false。未实跑、未结算、
账本42.667元不变；后续真实执行仍受用户停止实验的指令约束，不提前比较算法。

2026-11-07 用户在明确运行许可阻塞后主动恢复目标，继而暂停并再次恢复；据此继续
固定计划（对恢复操作的授权解释已单独记录，不伪造口头确认）。Readiness16条通过
原门槛，5/16独立成功、3个混合组、2家族反馈恢复；5个训练组20条产生3次实际更新。
当前在36/80确认边界资金等待，无pending。下一唯一阶段按[ADR0049](adr/0049-supplemental-closed-hour-coverage.md)
核查旧968项是否已被00–04整段账单覆盖；通过才补充分配并校正资金基线，冻结科学
源码/队列/判据和原始回执不改。新实验仍未进行test，不能宣称学习收益或开展算法比较。

2026-11-07 [真实执行与补充覆盖结果](trainable-agent-rl/supplemental-coverage-results.md)：
968次采样逐项覆盖及7个负例复核通过；9项财务事务故障测试通过。使用既有reconcile
补充分配已计入的9.72元整段上界，不增加provider费用、只释放3.342元重复估价；
历史14.79元及非采样预留不变。同步校正研究资金基线后，本轮新增差值仍5.612元。
确认边界当前占用44.937元、可用5.063元，无pending；继续同一冻结队列和checkpoint，
不因资金处理改变任务、policy、更新次数或判据。

### 2026-11-08：独立RL实验的单 seed 学习曲线阶段

用户确认新增100元上限、累计150元名义限额；首个Goal限定为同家族24组GRPO
与0/8/16/24检查点，三seed/新家族/算法对照不在当前阶段。依据
[ADR0050](adr/0050-grpo-learning-curve.md)和[冻结协议](trainable-agent-rl/learning-curve-plan.md)。
先420项判据检查，通过后再预算迁移和真实开发准入；旧实验与核心Runtime保持原范围。

2026-11-08用户继续授权同阶段v2：补独立尾斜杠私有判据、完整420项复验，通过后
落实已确认预算迁移及学习曲线；依据[执行记录](trainable-agent-rl/learning-curve-v2.md)。
v1停点与分数保留，不扩大算法/多seed范围。

v2结果：420项判据通过；24条真实开发诊断成功7条，混合/反馈家族门槛未过，
停止后续96训练及96验证。曲线调度与恢复仅离线契约验收；累计150/新增100档
已显式迁移，旧父账本封存、禁止重复子epoch。详见上述v2执行记录，不启动第三版。

### 2026-11-08：已打开开发数据的反馈v3修订

用户要求修订混合奖励/反馈修复不足后重新开发准入。本阶段仅6题公开反馈profile、
84项数据探针和固定24条开发，无新训练/算法比较。旧判据与分数保留，复用唯一150元
epoch，不重置新增100元档；本轮独立3元上限。依据[ADR0051](adr/0051-development-feedback-v3.md)
与[冻结协议](trainable-agent-rl/development-feedback-v3.md)。

开发反馈v3实测：84项数据检查通过，24条新开发8次成功、4混合G4覆盖2算法组，
3条有独立证据的反馈后修复覆盖2算法组，原准入通过。仅采样、无新训练客户端或
更新；Goal在此交付，不启动旧v2队列，后续训练/验证须独立冻结并验收。详见上述报告。

### 2026-11-09：通过开发准入后的学习曲线 v4

用户要求继续下一阶段。依据 [ADR0052](adr/0052-learning-curve-v4.md) 与
[冻结协议](trainable-agent-rl/learning-curve-v4.md)，复用封存 v3 准入，排除已打开开发题，
冻结 12 train/12 validation 的 v4 公开反馈。336 项数据检查后执行固定 96 条训练、
96 条验证及 0/8/16/24 尝试槽；继续唯一 150/100 元 epoch，阶段硬上限 60 元。
不扩展多 seed、新家族或算法比较。

v4 执行结果：336 项判据通过，192 行冻结及完整预算准入通过；真实运行在保存
初始 optimizer checkpoint 时出现 ActorEventSubmitError。只读核查恢复任务 ENDED、
0 checkpoint；新采样/训练更新均 0，学习效果未测。按未知事务门禁停止、保留
0.100 元 pending，累计保守占用 52.264 元，未重放/扩张。详见 v4 执行报告。

### 2026-11-09：v4 初始化的一次恢复

用户在停点后要求继续。按 [ADR0053](adr/0053-initialization-recovery.md)实现一次
独立目录恢复：旧任务须 owned/ENDED/0 checkpoint 且从未采样或更新；旧 0.100 元
pending 状态和金额保留，其他 pending 继续阻断。复用原 192 行科学队列与数据检查
证据，60 元上限继续锚定 52.104 元旧基线。新增结构化脱敏错误 capsule 与客户端
注册身份持久化；不自动重放旧请求、不递归恢复或改算法/判据。328 项实验回归通过。

恢复路径再次返回初始化 HTTP404/not_found；原 Train checkpoint 可下载且 optimizer.state
为空。用户要求继续实验、停止额外用量核对。按 [ADR0054](adr/0054-equivalent-empty-optimizer-start.md)
尝试一次相同 seed 的可验证初始状态重建；必须实际参数差0且空优化器组配置一致才可采样。
仍使用 v4 固定192行，旧失败与预留保留，拒绝将训练后非空优化器按此路径重建。

v4 run-03 完整执行结束：[结果](trainable-agent-rl/learning-curve-v4-recovery.md)。
192/192 条、17 次真实更新；固定四检查点 7→8→6→8（各24条）。最终配对 +4.17pp，
两个家族改善，探索门槛通过；95% 家族聚类区间 [−37.5,+25]pp，稳定收益未证实。
初始 adapter 差0、空优化器配置一致、最终权重差>0；全部G4与判据重复复核通过。
仍为单训练seed，不自动启动新家族、多seed或算法对照。


### 2026-11-09：固定规模 Base / GSPO 实验

用户明确要求按历史 GRPO 规模验证 GSPO 相对 Base 的提升。当前阶段限定为
[ADR0056](adr/0056-gspo-efficacy-study.md) 与
[冻结协议](trainable-agent-rl/gspo-efficacy-v1.md)：旧 expansion_v2 的40个G4训练组，
160训练、64验证、128测试轨迹；全新 Base/GSPO 配对采样。保留旧结果；旧测试集已打开，
本轮称固定基准复测。只评价指定 GSPO 变体相对 Base，不扩张到多 seed 或算法排名。


2026-11-09 Base/GSPO同规模实验已完成：[完整结果](results/gspo-efficacy-v1-2026-11-09.md)。
352/352真实轨迹、40个G4训练组、25次有效更新；验证10/32→18/32，
测试28/64→33/64（+7.8125pp；95%家族聚类区间[-1.5625,+17.1875]pp）。
单训练seed、既有测试集复测，不能宣称稳定收益或优于GRPO。反传查询失败后按ADR0057
从实际权重与完整Adam状态全等的检查点恢复，原失败保留、124条轨迹未重采；
358项实验回归及前后容器隔离检查通过。不自动扩大新seed或算法对照。

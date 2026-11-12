# Memory v2：P1 设计与实施契约

日期：2026-11-11。状态：设计交付，P2 未开始。依据：[P0 调研](research/memory-frameworks-2026-11-11.md)、[ADR 0031](adr/0031-progressive-project-memory.md)、[范围追加](extension-roadmap.md#memory-v2-范围追加2026-11-11)。本文的拟议工具、类型与配置目前均不可使用。

## 1. 目标与阶段范围

解决三个具体问题：有经验却不知道该查什么；只能依赖工具路径/错误发现经验；多次读取时把过多正文放入 Context。第一轮交付止于 P2：启动索引、主动搜索、按 ID 读取和适用性/权限/预算验收。

P0/P1 只交付文档与源码来源清单，不改运行时、依赖、数据库、配置默认值、独立评分器或历史报告。P3 结构化笔记与修订关联、P4 运行外整理、P5 真实收益实验是后续阶段，不能由本设计自动开始。

保留：显式 project_id/适用版本、SQLite 私有存储、每项目 100 条/正文 2 KiB、最多五条正文、证据 hash、人工批准、OS 隔离、版本化 Trace、原始事实不可覆盖。提议仍不能进入运行知识。

不纳入：Embedding/向量库、知识图谱、跨项目共享、原始会话搜索、自动批准/淘汰、后台 worker、第二个 observer Agent、Cron、训练、修改系统提示或自动生成 Skill。术语见 [GLOSSARY](../GLOSSARY.md)。

## 2. 四层信息与所有权

| 层 | 内容与事实源 | 所有者与读取方式 |
| --- | --- | --- |
| 原始事实 | 原始 Trace、Session、独立证据包 | 保持现有机制；模型不通过 Memory 读取隐藏判据或原始证据正文 |
| 项目记忆 | committed MemoryEntry，绑定来源、版本和批准 | MemoryService + Store；受控工具读取；提议和正式知识分开 |
| 记忆索引 | 从正式记录派生的主题卡片、词项索引 | 运行内生成，可丢弃重建，不存第二份权威数据库 |
| 运行上下文 | 索引卡片、适用正文及 provenance | ContextAssembly，较低信任资料，参与统一 Token 预算 |

当前目标、计划、未解决错误、修改文件属于任务状态；不因为需要跨轮恢复就复制到长期经验。代码中随时可查的事实优先留在仓库；难以重建的已验证经验才值得提议。

## 3. 授权、发现与适用性

必须分开两个判断，否则启动无路径时会出现“完全看不到主题”或“直接读到不适用规则”两个极端。

**发现资格**：记录已 committed、项目和版本严格一致、批准/修订绑定有效、来源可核验、内容策略通过、无主题冲突。索引和搜索可展示这些记录的卡片，即使本次尚未访问对应路径。卡片明确 `applicability=conditional`，列出路径与错误条件，不展示正文/自动摘要/匹配片段。

**正文适用性**：除发现资格外，必须满足当前可信工具历史产生的路径和错误条件，沿用 memory_query/select_memory 的精确路径/目录前缀与错误匹配。无条件的环境知识可以启动时读取。有条件条目先让 Agent 看到主题与条件，再通过正常工具观察相关文件/错误后读取。

模型输入的 query、paths/error_type 只能缩小搜索集合，不能建立“已访问文件”或“已发生错误”的事实。项目、版本、run_id、授权身份从可信组装传入，不接受模型覆盖。未授权/异项目 ID 统一返回 memory_not_available，不暴露异项目是否存在；当前项目的条件不满足可返回 memory_not_applicable。拒绝发生于 Store I/O 前。

发现时对同项目/版本的候选按 topic 检查冲突；不同正文 hash 则整组不暴露，同文只保留 ID 排序第一条。这个发现规则比 v1 的“已适用候选内冲突”更保守；v1 原路径不变。跨 topic 语义矛盾仍未解决，不自动合并。

## 4. P2 的 Interface 与返回契约

维持现有 `MemoryStore`、`MemoryEvidenceSource` 为持久化和证据 seam，不增加未有实际需求的外部框架 adapter。检索、去重、适用性、来源复查与裁剪集中在现有 Memory 模块，Loop 不理解 SQLite 或外部框架。

拟增加不可变、可序列化的值对象：

- `MemoryCard`：entry_id、topic、kind、paths、error_type、revision、content_sha256、evidence_sha256、approval_sha256、applicability。topic 是当前已批准字段；P2 不新增未绑定批准的 title/summary/tags。
- `MemoryReference`：entry_id、expected_revision、expected_content_sha256。避免把旧搜索结果误认作新内容。
- `MemorySearchResult`：snapshot_sha256、query_sha256、selector_version、cards、omissions、truncated、estimated_tokens。query 正文不另写普通日志。
- 正文读取结果：MemoryEntry 的安全公开字段、来源 run_id 和受信证据 hash；不含数据库路径、证据文件路径、隐藏判据或证据正文。

| 入口（均拟议） | 调用方 / 参数 | 结果与权限 |
| --- | --- | --- |
| `prepare_memory_index` | Context 准备执行器，无模型传入项目 | 最多五张卡片；memory.read + memory.evidence.read；隐藏但计账 |
| `search_memory` | 模型；query、limit，可选 paths/error_type 筛选 | 最多五张卡片、命中原因；同上权限；无正文 |
| `read_memory` | 模型；references，最多五个不同 ID | 逐条验证快照、修订、hash、来源与当前适用性；同上权限；正文总量有界 |
| `propose_memory` | 现有模型工具 | 保持现有 proposed-only 行为 |
| 六项维护工具 | 现有运行外操作者执行器 | 保持现有权限、证据与人工批准；不注册给模型 |

参数边界用 Pydantic，未知字段拒绝。query 非空，最多 256 字符且 UTF-8 最多 1 KiB；limit 为严格整数 1–5；paths 沿用最多 32 条、每条 512 字符及规范相对路径；error_type 最多 128 字符。references 不接受路径/批准字段。结果使用统一 Observation；条件排除属于结构化结果，Store 故障和授权拒绝不包装成“成功无结果”。

卡片与正文包含的所有文本字段都执行现有敏感内容/禁止材料检查。失败后不能部分泄露内容。哈希用于完整性绑定，不代表授权或语义正确。

## 5. 确定性词法检索 v1

P2 不引入模型查询重写、网络或 Embedding。每次 run 固定项目 Store 快照，词项索引仅在内存构建，源为至多 100 条记录，不能持久化到 Agent 工作区。

词项规则 `memory_lexical_v1`：Unicode NFKC + casefold；提取连续 ASCII 字母数字词项（下划线、标点作分隔）；汉字连续段生成相邻二元组，单字段保留单字；其他字母连续段作为词项。每个字段内去重，不使用隐式 stopword、词干化、随机数或当前时间。

对去重后的查询词项计算整数分数：topic 命中每项 8 分，paths 每项 4 分，error_type 每项 4 分，content 每项 1 分，各字段独立计分。空命中返回空结果，不回退“最近几条”；按 `(-score, entry_id)` 稳定排序。命中原因只列字段名与分数，不返回正文片段。paths/error_type 显式筛选采用现有精确规则，缺省不缩小发现范围。同义改写无共同词项时可不命中，作为已知限制固定在 case 中。

先进行项目/版本/状态/结构/冲突/内容检查，再排序、按 limit 截取，并复查所返回最多五条的来源。来源失败排除并记录受控原因，本次不从后续结果递补；不能因异常偷偷扩大 I/O 预算。启动索引不对用户任务做语义猜测，按 entry_id 稳定选前五张；完整发现通过 search_memory 完成。

候选计算可使用 private committed 正文构建词项，但只有经过来源复查的卡片能对模型可见。派生快照 hash 绑定记录完整指纹与 selector_version；外部不可用来源不修改原数据库状态。

## 6. Context、预算与失效

拟新增检索模式 `retrieval_mode = "progressive_v2"`，默认 `"deterministic_v1"`。旧配置解析、旧工具集合、旧 memory=None 和 mini-dsh-work 生成配置保持兼容；work 显式开启旧 Memory，不自动升级 v2。

v2 启动时调用受控 prepare_memory_index；之后每次模型准备阶段，通过同一个受控准备入口复查即将暴露的卡片/选中正文。每次实际执行都计一个工具调用及耗时；索引/分词缓存不能免除权限与证据复查。预算不足或准备读取失败时停止本次模型请求并产出结构化失败/预算终态，不降级成绕过核验的记忆。

模型显式 read_memory 的结果按正常工具历史留在事实记录；Context 中的正文投影只保留最近选中的至多五条，查询变化不把所有历史命中累加为常驻前缀。卡片与正文都附来源元数据，置于当前任务/关键状态之后，低于系统/项目约束。给已有 Context 策略同一组投影，使用同一个 count_tokens；不另建 Context 策略。

P2 默认硬上限：启动索引 512 估算 Token，单次搜索结果 512，单次正文读取 2,048，Context Memory 总投影 2,560；可配置值只能下调。计入包裹文本和来源字段，单条正文仍为最多 2 KiB。按顺序保留完整条目，超限从尾部丢弃，记录 omission_reason=token_limit；单条无法容纳不截断成可能失真的规则。完整结果同时受现有工具输出 bytes 上限约束。模型请求的其他受保护内容不能被 Memory 挤掉，最终超过输入预算时不调用模型。

正文工具历史可能随策略保留更久；在每次请求派生视图中，不再适用、证据变更或失效的 Memory 结果必须替换为带 call_id 的不可用标记，保留调用/结果配对和原始 Trace。memory_content.py 的事实保护不因过滤机制失效而放开。实现必须验证这一点，不能只从常驻前缀删除就声称撤回完成。

同实例运行/维护互斥继续保留；run 结束清理快照与选择结果，不跨 run 复用授权。现有存储跨进程事务锁不等于跨进程撤回通知。P2 在每次暴露前受控读取当前项目记录，检查原引用的完整身份，发现修改/拒绝则不再暴露；不把外部新记录自动并入本次冻结快照。外部 writer/恶意同 UID 程序仍不在保证内。

取消后不再开启新 I/O；等待已在执行的受控 Store 工作结束再清理，遵循原有提交可读回契约。v2 全部读取过程不应改变数据库 bytes，也不新增宿主索引文件。

## 7. 事件与可观测性

复用 tool_call_started/Observation/权限事件与 Context provenance，不建立第二套 Trace。拟新增 schema_version=1.0 的 memory_index_prepared、memory_search_completed、memory_read_completed 事实事件：run_id、step_id、call_id、selector_version、snapshot hash、引用的 ID/revision/hash、命中字段/分数、排除原因、耗时、Token 估算与截断标记。耗时用单调时钟，事件时间用 UTC。

事件不记录 query/经验正文、私有路径、配置或证据全文；工具调用参数/结果进入 Trace 前沿用现有敏感值保护。涉及查询 Secret 时拒绝且不把原值写入 Trace。筛选拒绝与安全拦截不同，不从 omission 数推导安全指标。

## 8. 文件范围、迁移与回退

| 模块 | P2 拟改动 | 保持的职责 |
| --- | --- | --- |
| domain/memory.py | 卡片/引用/检索结果、纯筛选与词法排序 | 无 I/O，无第三方 SDK，MemoryEntry schema 1 不变 |
| domain/memory_content.py | 检索文本与派生结果的内容保护复用 | 不将派生 Memory 当新的原始材料 |
| ports/memory.py | 运行内只读视图按实际调用最小扩展 | 不把 Store 或路径暴露给模型 |
| runtime/memory.py | 三种读取入口、引用核验、快照与当前记录复查 | 状态链、权限、证据、维护互斥集中处理 |
| application/context_assembly.py、memory_query.py | 组装索引/正文、可信匹配线索与计账 | 不加入 SQLite/文件细节，不改变 Loop 状态机 |
| composition.py、config.py | 显式模式与有界配置，工具集合 | 两条组装入口与 AgentFactory 一致 |
| adapters/memory.py、memory_evidence.py | 原则上复用；只有 case 证明契约缺口才改 | POSIX/私有路径、原子发布、来源 bytes 核验 |
| tests/test_memory_*.py、test_config.py、test_composition.py | 先写契约再实现；跨层用真实 Store 临时目录 | 独立判据留在 Agent 工作区外 |
| docs、examples/project_memory_demo.py | v1/v2 配置与离线示例 | 不改历史封存或 M5 源码身份 |

P2 无 schema/数据库迁移；所有旧 committed 条目的 topic/正文已绑定原批准，索引只派生。回退为删除 retrieval_mode 或设 deterministic_v1，新增工具不注册、运行内索引丢弃；关闭 memory 后不读取 Store。旧程序未知新字段会拒绝，回退前须移除新配置字段；不能承诺新配置能被旧二进制解析。P3 若需新增持久化字段，另出 schema/批准 hash 兼容 ADR，不回填虚假批准。

## 9. P3–P5 的设计方向与启动条件

P3：结构化触发条件/规则/例外/验证方式，区分文本说明与真实验证事实；支持人工替代与修订关联。新增多来源证据必须显式记录所有来源，独立验证器给出受信结果；不能用旧单来源证据给合并主张背书。完成新 schema/迁移契约与 case 后再实现。

P4：操作者显式执行 memory consolidate，单 Agent、单进程、顺序处理已授权脱敏材料。每次有独立 run_id 和步数/工具/Token/墙钟预算，模型调用仍走现有 Model port。只生成绑定输入 hash、原修订和来源的提议变更集；批准、提交、reject 仍是操作者后续动作。中断可留下完整 proposed，不能半修改 committed；不自动重试写入。原始 Trace 不改，隐藏判据只由可信验证器检查，不能交给整理模型。P4 不开后台调度。

P5：运行前冻结任务/种子/判据/预算与分母，分别比较索引、主动检索、整理三个单变量，不用不同源码混淆机制收益。任务成功以独立验收为准；每个对照计划至少三次配对重复并报告差值与不确定性，完整协议需 P5 前另行冻结。没有证据不计算收益，P0/P1 不执行真实调用或承诺收益。

## 10. 出关条件与当前状态

P0/P1 出关：固定来源清单、明确取舍、设计/ADR/主计划一致、兼容和迁移清楚、P2 覆盖矩阵包含可执行判据及预算/隔离约束、文档链接有效，四项工程门禁有当前结果。具体核验记录在 [交付记录](results/memory-v2-p0-p1-2026-11-11.md)。

P2 出关：先落实 [冻结验收设计](memory-v2-acceptance.md)的 executable checker 与夹具再实现；所有必测项和真实副作用探针通过，两条生产组装/七种 Context 策略回归兼容，四门禁通过、离线示例可运行。未触发功能保持未测；源码和证据单独冻结。P2 通过后停止，不自动进入 P3。

目前 P2–P5 功能均未实现/未测；此文只能证明设计可审阅，不能证明新记忆工具已经可用。

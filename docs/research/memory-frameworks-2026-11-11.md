# Memory v2：P0 开源源码调研

日期：2026-11-11。范围：为 Mini-dsh 的项目经验发现、按需读取和运行外整理选择参考机制。
结论：保留现有受控 SQLite 和批准链；借鉴 ReMe 的可重建视图、claude-mem 的渐进读取、LangMem 的提取与存储分离、Hindsight 的来源关联。当前不引入这些框架为运行依赖。

概念参考：[Anthropic — The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)（页面标注2026-11-06发布，2026-11-11复读）。文章区分transcripts、notes、representations、injected context，并讨论运行外整理用于纠错和重组；它是研究材料，不能据此认定全部机制都已用于Claude Code。下面将这些层映射到Mini-dsh事实源、受控经验、派生索引和Context，是本项目的设计推导；不复用文章benchmark成绩证明本项目收益。该网页没有仓库commit，未纳入七个开源项目的源码hash保证。

## 1. 方法、版本与结论边界

先用官方仓库/文档发现入口，再通过 `git ls-remote <url> HEAD` 解析提交，下载该提交的 codeload 归档，静态阅读源码。GitHub REST 接口曾返回 rate limit exceeded，随后使用 Git 传输获取提交，没有用浮动 README 替代版本身份。

[源码清单](memory-frameworks-2026-11-11.sources.json)固定七个 commit、归档 SHA-256、39 个所引用文件的 SHA-256、大小及永久链接。四个重点项目作定向源码阅读；三个补充项目只核对所列入口。未安装所调研框架、运行其测试或启动服务；未提供凭据、调用模型、复现实验，也未测真实网络流量、资源峰值与取消后的副作用。许可证名称来自该提交根 LICENSE，不构成传递依赖许可证或法律审查。

| 项目 | 固定 commit | 根许可证 | 阅读深度 / 处置 |
| --- | --- | --- | --- |
| ReMe | `55a3bb7b0f868c95a14721301475bb3b91eab612` | Apache-2.0 | 重点；借鉴文件知识组织、索引和分批整理 |
| claude-mem | `6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb` | Apache-2.0 | 重点；借鉴启动索引与按 ID 读取 |
| LangMem | `8620b986af5b0c7593e6b7fbece2998690d22d15` | MIT | 重点；借鉴提取结果与持久化分离 |
| Hindsight | `586f0faed1e39e8334d999d2545d1ebafb1c5b64` | MIT | 重点；借鉴经验整合的来源关联 |
| Mem0 | `f1791daa86d469249c9b002f62644d58a1eb96a0` | Apache-2.0 | 补充；暂不集成 |
| Graphiti | `ebef986a0b2360211b65751c111cacb4a7d790eb` | Apache-2.0 | 补充；暂不集成图数据库 |
| Letta Code | `ad308cd3cb8486afa0bb8ad624c690b828339ea7` | Apache-2.0 | 补充；参考完整 Harness，不替换本项目 Loop |

复核者可从清单逐项获取 `https://raw.githubusercontent.com/{repo}/{commit}/{path}`，对响应 bytes 计算 SHA-256 与 `reviewed_files` 比较；或 `git fetch origin <commit>` 后用 `git show <commit>:<path>`。归档 hash 是本次下载 bytes 的记录；上游重新打包可能改变归档 hash，逐文件 hash 和 commit 用于内容复核。不要运行上游安装器或导入模块来做这项检查。

## 2. 四个重点项目

### ReMe：可编辑知识与派生索引

源码依据：固定版本的 [SearchStep](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/steps/index/search.py)、[LocalFileStore](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/components/file_store/local_file_store.py)、[默认配置](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/config/default.yaml)。搜索支持过滤和链接扩展；默认配置禁用 embedding store。文件索引在内存工作并有压缩持久化。

整理实现分成 [extract](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/steps/evolve/dream/extract.py)、[integrate](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/steps/evolve/dream/integrate.py)、[finish](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/reme/steps/evolve/dream/finish.py)：读取变化材料、整合知识、记录处理进度。integrate 暴露 write/edit 等工具且有两次尝试；extract 明确说明材料分批上限不是请求 Token 硬上限。

Mini-dsh 取舍：借鉴可重建索引与分批处理；P2 保留私有数据库，不把可编辑 Markdown 作为批准事实源。P4 仅产出变更提议，不能照搬直接改写与副作用重试。[依赖声明](https://github.com/agentscope-ai/ReMe/blob/55a3bb7b0f868c95a14721301475bb3b91eab612/pyproject.toml)包含服务、调度及可选 Agent/向量依赖，当前不整体引入。

### claude-mem：编码会话的渐进读取

源码依据：[ContextBuilder](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/services/context/ContextBuilder.ts)、[ContextBudget](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/services/context/ContextBudget.ts)、[MCP 搜索入口](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/servers/mcp-server.ts)。启动内容有预算裁剪；搜索入口区分索引、时间邻域和观察正文。[SearchOrchestrator](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/services/worker/search/SearchOrchestrator.ts)支持 SQLite 与 Chroma 策略及回退。

Mini-dsh 取舍：借鉴“先发现 ID，再取正文”，P2 不开放时间邻域或原始会话搜索，以免把隐藏判据和工具原文带入 Memory。[worker](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/services/worker-service.ts)和 [SessionManager](https://github.com/thedotmack/claude-mem/blob/6b779c06796138aeb4b0ee2fa7dbd25912e4f7fb/src/services/worker/SessionManager.ts)包含观察处理、遥测、取消和恢复逻辑，不能视作一个无副作用检索库。当前不引入独立 worker、observer 或其宿主目录默认值；其内部超时/重试不能替代本项目统一预算和进程清理验收。

### LangMem：提取与写入的分离

源码依据：[knowledge/extraction.py](https://github.com/langchain-ai/langmem/blob/8620b986af5b0c7593e6b7fbece2998690d22d15/src/langmem/knowledge/extraction.py)中的 MemoryManager 返回结构化提取结果，默认 `max_steps=1`；同文件的存储管理实现负责持久化。[knowledge/tools.py](https://github.com/langchain-ai/langmem/blob/8620b986af5b0c7593e6b7fbece2998690d22d15/src/langmem/knowledge/tools.py)的管理工具允许配置 create/update/delete 动作，并使用 BaseStore。

[reflection.py](https://github.com/langchain-ai/langmem/blob/8620b986af5b0c7593e6b7fbece2998690d22d15/src/langmem/reflection.py)提供本地线程队列与远程执行入口；本地队列检查取消标记后调用 reflector。不能仅据此认定正在执行的模型/写入能够及时取消。[依赖声明](https://github.com/langchain-ai/langmem/blob/8620b986af5b0c7593e6b7fbece2998690d22d15/pyproject.toml)包含 LangChain、LangGraph、模型 SDK 和 LangSmith。

Mini-dsh 取舍：借鉴纯提取候选与受控写入分开；不使用默认管理工具直接更新正式知识，不将 LangGraph 引入核心。未来若评估提取器 adapter，仍须通过本项目 Model port 和受控执行端口计账，不能让库内部模型调用绕开 Trace。

### Hindsight：带来源的整合观察

源码依据：[consolidator.py](https://github.com/vectorize-io/hindsight/blob/586f0faed1e39e8334d999d2545d1ebafb1c5b64/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py)区分原始事实、整合 observation 和 mental model，关联 source memory IDs、支持证据数量与变更历史，并处理来源变化及并发取消。[recall.py](https://github.com/vectorize-io/hindsight/blob/586f0faed1e39e8334d999d2545d1ebafb1c5b64/hindsight-api-slim/hindsight_api/engine/memories/pg/recall.py)包含语义/BM25 与时间检索。[reflect agent](https://github.com/vectorize-io/hindsight/blob/586f0faed1e39e8334d999d2545d1ebafb1c5b64/hindsight-api-slim/hindsight_api/engine/reflect/agent.py)有迭代、上下文和工具结果预算。

Mini-dsh 取舍：借鉴来源引用与“证据变化后重新考虑结论”，不把支持数量当成独立验收或人工授权。P3/P4 多来源整合需设计新的证据包；当前单一证据 hash 不能冒充完整的多来源证明。[服务依赖](https://github.com/vectorize-io/hindsight/blob/586f0faed1e39e8334d999d2545d1ebafb1c5b64/hindsight-api-slim/pyproject.toml)包含数据库驱动、模型 SDK、HTTP/MCP 与向量能力；目前不引入其服务栈。

## 3. 三个补充项目与候选方案

下表为上述固定源码的集成审查，不是运行测量。模型调用次数指入口可能触发的内部工作，不能按一次 add/reflect 当成一次推理计账。

| 项目 | 模型与网络 | 写入、取消与重试 | 本轮仍未知 |
| --- | --- | --- | --- |
| ReMe | 搜索可不调用模型；Dream 的提取/整合会分批调用 agent wrapper；core extra 有多种模型/向量依赖 | integrate 持有应用内锁并直接编辑文件、可再次尝试；background_job 有限时等待再取消 | 一次完整整理的总调用数/Token、失败后的实际文件状态、是否出网与内存峰值未实测 |
| claude-mem | 独立观察处理和可选 Chroma；worker 存在遥测调用入口 | SessionManager 有 abort/超时等待和恢复调度；不是本项目的统一预算 | 观察器内部总调用、退出后真实子进程、关闭遥测后的真实出网未实测 |
| LangMem | 提取器 max_steps 有界，但调用的 trustcall/模型 SDK 仍有内部行为；包依赖 LangSmith | 管理工具可直接写/删 BaseStore；线程队列取消不等于在途推理/事务回滚 | SDK重试与追踪配置、在途取消效果、真实存储副作用未实测 |
| Hindsight | reflect 有检索与推理循环；服务栈包含数据库、模型与遥测依赖 | consolidator 处理来源/并发并写整合结果；有取消兄弟任务的逻辑 | 整个retain→整合→reflect的调用总额、真实事务/网络/资源限额未实测 |

对应入口已在源码清单固定；没有发现某项网络行为不能推导“离线安全”。若后续选择集成，必须在独立临时环境进行请求捕获、实际文件/进程检查和预算故障注入后再签运行兼容。

- Mem0：[memory/main.py](https://github.com/mem0ai/mem0/blob/f1791daa86d469249c9b002f62644d58a1eb96a0/mem0/memory/main.py)提供 add/search/update 等入口；[telemetry.py](https://github.com/mem0ai/mem0/blob/f1791daa86d469249c9b002f62644d58a1eb96a0/mem0/memory/telemetry.py)的遥测开关默认开启。当前不采用其默认初始化与自动写入路径；此处没有验证关闭遥测后的真实出网行为。
- Graphiti：[graphiti.py](https://github.com/getzep/graphiti/blob/ebef986a0b2360211b65751c111cacb4a7d790eb/graphiti_core/graphiti.py)提供 episode 摄取、图检索与模型/数据库客户端组装。图关系和时间能力有参考价值，但当前百条经验的目标尚无图检索需求；暂不引入数据库与实体抽取。
- Letta Code：[固定 README](https://github.com/letta-ai/letta-code/blob/ad308cd3cb8486afa0bb8ad624c690b828339ea7/README.md)描述 memory、MemFS、sleeptime 与完整 Harness；这里只核对定位、package 和许可证，未对其运行实现做安全或效果评估。不能拿历史 MemGPT/Letta V1 的行为代表该提交。

| 方案 | 好处 | 对本项目的代价 | 决策 |
| --- | --- | --- | --- |
| 整体接入外部框架 | 直接获得更多检索/整理能力 | 嵌套模型调用、后台任务、数据写入与依赖需重新纳入权限/预算；维护两套状态语义 | 当前不采用 |
| 只接入外部检索/提取 adapter | 将来可替换具体算法 | 仍需处理隐式网络、输出契约及供应链；当前规模缺少必要性证据 | 保留后续实验可能，不预建 adapter |
| 在现有 Memory 中实现小型索引与受控工具 | 复用批准、证据与隔离；容易确定性验收 | 需要自行实现检索与维护，词法匹配有同义表达缺口 | P2 推荐方案 |

## 4. 与本地实现的逐项差距

本地基线为 `a7c5d3bb0eff990e701051e6262a62a82bfbf349`；原 Phoenix 工作区未参与。当前源码文件 hash 见清单 `local_sources`。

| 需求 | 现有机制 | 设计动作 |
| --- | --- | --- |
| 正式知识与提议分离 | domain 状态链、维护工具、来源核验均已有 | 保留，P2 不迁移 schema 1 |
| 启动时发现经验 | Context 通过 retrieve_memory 按工具路径/错误读取；无路径/错误约束的 committed 条目也可命中 | 增加无正文索引；不是宣称当前启动永远无记忆 |
| 模型主动搜索 | 模型端目前只有 propose_memory | v2 可选 search_memory/read_memory；维护能力仍不可见 |
| 相关性与适用性 | select_memory 做项目/版本/路径/错误筛选，按 ID 排序，最多五条 | 增加词法排序；区分“发现存在”与“正文当前适用” |
| 记忆修订 | revise 改正文、升 revision 并清授权；reject 失效 | P3 再设计结构化笔记和替代关系 |
| 自动整理 | 没有整理器，终态不写 Memory | P4 手动、有预算、只生成候选 |
| 默认开关 | 通用 memory=None；mini-dsh-work 显式生成 memory 配置 | 保持两种入口语义；新检索模式另行 opt-in |

## 5. 本轮交接

P0 没有证明任何外部项目的效果或与 Mini-dsh 的运行兼容性。没有直接复用第三方源码，不新增依赖或模型服务。P1 的完整设计、文件范围、迁移、P2 case 和停止条件见 [Memory v2 设计](../memory-v2-design.md)；最终核验见 [P0/P1 交付记录](../results/memory-v2-p0-p1-2026-11-11.md)。

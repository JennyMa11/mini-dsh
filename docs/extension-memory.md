# Project Memory：PR 4 开发记录

PR 3 已验收并封存，见 [阶段报告](results/extension-pr3-2026-10-29.md)。本阶段依据 [主计划](extension-roadmap.md) M01–M04/E03；功能、兼容性和出关结论以 [PR 4 独立报告](results/extension-pr4-2026-10-30.md) 为准。本文记录接口、迁移限制和开发证据。原有 benchmark/memory.py 统计进程内存，与这里的项目知识无关。

本文保留开发阶段的历史状态；文中的未批准、0/90 等读数描述当时版本。实际种子授权、真实队列及最终停止结论见 [M5 报告](results/extension-m5-2026-10-30.md)，不反向改写历史审阅包或 Trace。

## 已实现的组件边界

`domain/memory.py` 提供不可变 MemoryEntry、来源证据值对象和 proposed → validated → approved → committed 的基本状态链。记录绑定显式 project_id、topic、适用路径/错误类型、内容、来源 run_id、证据 hash、适用版本、schema_version、修订号及批准身份/UTC 时间。topic 表示同一项目内的知识主题，用于确定性冲突检查；不能把不同表述简单推定为语义一致。

验证要求来源明确存在并且显式 accepted=True，字符串或数字不能代替布尔事实。修复/诊断要求同项目、同适用版本、同 run 的 independent_acceptance 证据；环境约束要求可追溯的操作者配置确认。

`adapters/memory_evidence.py` 已实现 JsonMemoryEvidenceSource，调用方显式登记 evidence_id 与证据包相对路径/hash。独立验收包绑定项目、版本、来源 run 及四类文件引用：现有 ScoringEvidence sidecar、已结束 Trace、判据、至少一个实际产物；每个引用都有 hash。读取检查 task_success 严格为 true、case_id、run_id、判据来源、Trace 唯一开始/结束及结束后无模型/工具执行事实，再核对实际文件 bytes。环境包绑定配置文件/hash、确认身份和 UTC 时间。包内不能夹带任意额外字段；模型不能提供路径或 accepted 标记来替换注册授权。

证据根目录在工作区之外；读取使用目录描述符和逐级 O_NOFOLLOW，拒绝符号链接、硬链接、非普通文件、变化的目录身份和不符的 hash，单文件上限 8 MiB、产物引用最多 16 个。不支持安全 dir_fd API 的平台拒绝启用该 reader。来源只返回元数据与证据包 hash，判据、Trace、配置及产物正文不进入 Memory/Context。reader 复用独立验证器的结论并核验来源，不代替验证器重新评分；组件测试中的 synthetic_fixture 不是实际任务验收成绩。

批准绑定当前内容 hash、完整提议指纹和修订号。正文修改递增修订并清除旧证据绑定、验证及批准；人工 reject 使记录失效并清除授权，旧提交不能覆盖失效状态；未经批准状态不能夹带批准字段。该 hash 是完整性绑定，不能代替操作者身份授权。测试使用 fixture_operator，未代表用户批准任何真实 Memory 或评测种子。

`ports/memory.py` 定义持久化接口；`adapters/memory.py` 使用标准库 SQLite。调用方显式授予一个私有目录和文件，不读取隐式主目录，也不从工作区 basename 推断项目。create 只接受 proposed；get 按 project_id/id 查询；snapshot 返回一个项目的不可变、限额快照；replace 使用完整旧记录作条件更新，拒绝跳级、内容偷换和过期状态。相同批准对应的完整 committed 记录可以重复提交；后续修订或失效后重试旧提交会被拒绝。单项目第 101 条新增拒绝，既有记录不淘汰，另一个项目的额度独立。

异步存储按实例串行执行，并在取消后等待线程结束，再释放操作锁。提交前取消检查已触发时回滚；提交期间或之后取消可能留下完整已提交记录，调用方须按项目/id 读回确认，不能把 CancelledError 推断成回滚。close 拒绝后续操作，并等待进行中的线程。同步 transaction_observer 仅用于存储故障注入，收到 before_storage_read/before_storage_publish/before_initialize_publish/before_commit/after_commit 固定阶段名；提交前故障回滚，提交后的通知故障返回 memory_committed_notification_failed，不能误报已回滚。初始化在内存 SQLite 中建立完整 schema，再将序列化文件通过受控私有临时文件和排他链接发布，不覆盖既有数据库；发布前故障清理临时文件且不留下半初始化数据库，重新打开可正常创建。已有未知 schema 或空文件仍拒绝采用，不以自动重建掩盖损坏。这些检查不等同于进程崩溃或断电验收；路径竞争另有实际文件替换回归。

纯函数 select_memory 只保留 committed、项目和显式适用版本一致的记录。路径使用规范化的相对 POSIX 字符串，精确匹配或目录前缀匹配，大小写敏感；配置了 error_type 时须与本次错误类型一致，未提供匹配线索不能推测。先排除不适用项，再检查当前候选中同 topic 的正文 hash：分歧时整组排除，同文时保留 entry_id 排序第一条，最后最多返回五条。排序不依赖输入顺序和时钟，遗漏返回条目 ID 与原因。该冲突规则依赖人工维护的稳定 topic，不能检测不同 topic 之间的自然语言语义矛盾。选中项保留原始来源、证据 hash、修订和批准；已通过受控准备连接 Context。

`runtime/memory.py` 提供可选组装的 MemoryService/ProposeMemoryTool。专用 memory.propose 授权通过后，工具才访问 Store；模型只提供 kind/topic/content/paths/error_type，项目和版本来自服务构造参数，run_id 来自执行器。提议工具不返回正文、数据库路径或批准能力，状态始终是 proposed。活动 run 必须由可信调用方显式包围，运行前、运行后和其他 run 的提议均拒绝。build_local_runtime 和 build_configured_runtime 已绑定整个 run 生命周期，包括开始/结束 Hook 与清理；Conversation AgentFactory 也使用同一包装。

`runtime/memory_maintenance.py` 的 memory_maintenance_tools 只供运行外的操作者执行器注册：review_memory、validate_memory、approve_memory、commit_memory、revise_memory、reject_memory。它们要求 memory.maintain；校验、批准和提交还要求 memory.evidence.read。批准身份由可信调用方绑定，参数不能覆盖；审阅返回限长正文、当前修订、内容/hash/证据及批准元数据，批准输入显式 hash、修订和 UTC 时间。批准/首次提交前重新读取 hash 绑定的来源；相同完整 committed 记录的重复提交返回原状态。校验、批准、首次提交均检查同版本/同 topic 的已验证分歧；不同正文的竞争记录被拒绝，未验证提议不阻塞。修订与失效清权，失效可重复执行。

维护期间阻止新 run 和并行维护；运行期间拒绝全部六个维护工具，取消会释放维护标记。当前运行/维护互斥覆盖同一 MemoryService 实例；SQLite replace 在目录描述符 advisory lock 内读取当前完整库，再在内存 BEGIN IMMEDIATE 事务中检查项目/版本/主题冲突并原子发布，防止不同实例或进程覆盖相互冲突的条目。两个实际实例竞争的回归读回恰好一条 validated、一条 proposed；独立进程持锁时竞争者明确返回 busy，释放后两条完整记录保留。旧版不一致数据另由显式故障夹具验证服务拒绝。所有测试批准身份是 fixture_operator，没有替用户批准任何真实条目。

RetrieveMemoryTool 只供受控 Context 准备执行器注册，要求 memory.read/memory.evidence.read，返回 ID、修订、hash 和遗漏原因，不返回正文。它冻结本次 run 的项目快照，先按查询筛选，再复查最多五个候选的来源；已提交但证明变化或未登记的记录保持原状态、以明确原因排除。MemoryView 只暴露运行内只读选择结果，run 结束清理；不同 run 不能读取旧视图。

ContextAssembly 已有可选 memory 参数，默认 None 不读取 Memory；有 Memory 时仅通过 retrieve_memory 准备。查询使用最近 32 个规范相对工具路径和结构化工具错误/失败命令的 Python Error 诊断；不从任务文本猜测路径。查询改变重新计账读取，相同查询复用视图。选中项以较低信任 User 消息放在当前任务/关键状态之后，保留来源 run、证据 hash、版本和修订；遗漏也进入 provenance。Memory 正文不反写原始 history，前缀纳入统一 Token 计数与可调策略预算。七个既有 Context 策略均已通过实际 Loop、私有证据核验及连续两次 run 的检索/来源检查；全局预算的 Memory 轨迹已检查：隐藏读取耗尽工具额度、输入上下文超限、权限拒绝时模型不调用；响应后已知超量 20 Token 或未知用量时禁止后续提议，私有数据库字节不变，维护守卫释放。Token 名义预算根据响应后用量执行，不能与请求输入上限混为一谈。

提议边界限制正文字符数，并在 domain 再检查 UTF-8 2 KiB 上限；路径最多 32 条、每条最多 512 字符，error_type 最多 128 字符。项目/来源/批准不能由模型字段覆盖。敏感项沿用项目 redact_text 的已知 Bearer/赋值及 JSON/Python repr 引号字段规则（含转义字段名），检查正文、来源、路径、错误类型及批准身份。假 password 探针覆盖正文/路径/错误字段，检查数据库未创建以及实际 JSONL Trace 不含 canary；不能据此宣称识别任意编码或无标记 Secret。

`domain/memory_content.py` 增加有界的已知材料指纹检查：显式 forbidden_content_sha256、原始任务、已观察的 system/user/tool 消息、正文行及 JSON 字符串值；识别原文、首尾空白和规范 JSON 的相同内容，不做语义判定。缓存只保留 hash；每个观察视图最多 4096 个指纹，超额拒绝提议。提议保存当时的指纹快照，后续修订/授权检查可复用，最多缓存 100 条记录。Assistant 自行撰写的文本不作为原始材料；派生 project_memory User 段不反向污染来源检查，工具即使同名也不能绕过。非法 UTF-8 的任务不会留下半启动 run。生产 before_model/after_tool 已接线，同一响应中读取文件再立即提议原文也会拒绝。不可解码观察将本次来源检查标记为失败，后续提议拒绝，即使观察 Hook 异常被公共调度器抑制。已知禁止标签会在下一次模型请求的派生视图脱敏，保留调用 ID/结果配对；不能改写的 system/user 消息命中则停止请求。原始事实历史不被改写。

MemoryConfig 与 resolve_config 已有显式项目/版本、绝对私有数据库/存储根、可选证据根/授权和禁止材料 hash；解析不创建目录或数据库。默认 memory=None；完整 memory 表显式配置，false 可关闭，MINIDSH_MEMORY 接受 JSON 对象/false/null，CLI 高于环境，高于文件。嵌套输入严格拒绝未知批准字段、相对宿主路径、缺失证据根或无效 hash，并隐藏错误中的私有值。两条生产组装入口已消费该配置。Memory 默认仍为 None；开启后模型工具集合仅增加 propose_memory，retrieve_memory 只在 Context 准备执行器内，六项维护工具只在 runtime.memory_maintenance(operator_id) 返回的操作者执行器内。显式插件 Store/Evidence 通过 ports 注入，不因对象的布尔值为 false 而退回默认 adapter。

数据库必须在 Agent 工作区之外；已检查真实路径、链接文件/硬链接与目录身份，避免仅凭大小写字符串判断。Store 绑定已授权目录的设备/inode 身份，拒绝目录被同路径替换。当前 adapter 不再将宿主文件路径传入 SQLite：通过授权物理目录描述符和 O_NOFOLLOW 读取，SQLite 仅操作内存，原子发布也锚定同一目录。读取前和发布瞬间替换目录/符号链接的回归已检查外部字节不变；提交完成后路径绑定丢失时返回 memory_committed_notification_failed，不能推断回滚。该约束不构成 OS Sandbox：能在宿主执行代码的进程仍可能访问外部文件，运行时接入必须解决这一点，见 [ADR 0026](adr/0026-private-project-memory.md)。

## 生产使用边界

包含进程能力（含 Git 和固定验证）时，Memory 必须使用已隔离的执行世界。默认 Docker 组装或可信调用方显式提供 `RuntimeEnvironment(..., host_storage_isolated=True)` 才能启用；该布尔值是宿主 adapter 的隔离声明，不可交给模型配置。仅路径工具的本地模式要求已知 LocalFileBackend；未知 adapter 和未隔离的进程能力拒绝组装。默认 Docker 后端也已完成独立正向开发探针；这些开发记录仍须在阶段冻结后重新验收，不能外推到未经检查的部署。

最小文件工具配置（先由操作者创建工作区外的私有目录）：

```python
options = RuntimeOptions(
    enabled_tools=frozenset({"read_file", "propose_memory"}),
    memory=MemoryConfig(
        project_id="my_project",
        applicable_version="v1",
        database="/absolute/private/memory.sqlite3",
        storage_root="/absolute/private",
    ),
)
```

没有受信证据授权时仅能保存 proposed，不会成为 Context 知识。运行结束后可通过 `runtime.memory_maintenance("operator_id")` 审阅，再显式 validate/approve/commit；批准绑定所审阅内容的 hash 和修订。配置来源与修复来源按前述不同证据核验，六项维护能力从不出现在模型工具列表。关闭 Runtime 时须 await runtime.close()。

## 统一验收入口（真实调用受阶段与人工批准门禁保护）

`scripts/extension_acceptance.py` 已提供四个子命令。freeze 仅收集明确 allowlist 的源码/测试/夹具/文档和根文件，核对原 M0 输入 hash，生成确定性归档与清单，不读取凭据；check 同时核对归档 bytes、成员、当前源码与冻结输入，`--archive-only` 只检查历史包。run 默认复用 BenchmarkRunner 顺序执行六题的 Baseline/A 各一次，使用固定响应脚本；比较完整模型请求、工具动作、Token、终态、判据及产物 hash。该离线入口只重放受信固定脚本，本地后端不作为 OS 隔离证据。

```bash
uv run python scripts/extension_acceptance.py freeze --output benchmark-results/extension-v1/my-snapshot
uv run python scripts/extension_acceptance.py check --snapshot benchmark-results/extension-v1/my-snapshot
uv run python scripts/extension_acceptance.py run --snapshot benchmark-results/extension-v1/my-snapshot --output benchmark-results/extension-v1/my-offline
uv run python scripts/extension_acceptance.py report --snapshot benchmark-results/extension-v1/my-snapshot --evidence benchmark-results/extension-v1/my-offline --output benchmark-results/extension-v1/my-derived
```

所有输出目录必须全新；失败、取消与未运行记录不能被后续执行覆盖。report 仅读取归档/Trace/独立 sidecar，复用 score_harness 与 aggregate_harness_metrics，记录输入与评分器 hash；保留 90 行预声明机会及未知项。离线模式只有 12 行新证据，其余 78 行保持 not_run，不计成真实模型样本。真实模型调用仍为 0/90。report 不自动签署 28 行覆盖矩阵，也不推断机制收益。

E03 的确定性检查覆盖未运行、完整已知消耗、未知用量、未结束 Trace、失败消耗和已知超限（名义 100、实际 120、超量 20）；用量不完整时完整总量及每次成功 Token 为 null，已知部分单独保留。`run --isolated-matrix` 使用脚本模型按冻结顺序执行完整 90 行容器队列，供接线演练。`run --provider deepseek` 使用相同执行路径；只有种子含真实人工批准、批准绑定原协议 hash，且 PR 4 的 28 行验收证据全部签署后，才读取显式凭据。每个批准包仅能预留一次真实队列，失败和未知消耗停止后续调用，不能重新冻结以重置额度。完整脚本队列已跑通，当前没有真实调用。

## 五组组装与教学种子

`benchmark/extension_matrix.py` 提供固定五组配置和统一隔离运行组装：Baseline/A 的选项完全一致，B 仅增加 Skills，C 仅增加 Verification，D 仅增加 Memory 配置。所有组必须由可信调用方提供同一已隔离文件/进程环境；普通本地环境在模型/Session I/O 前拒绝。全部组的可见 test 定义同为 30 秒，避免把命令超时差异混入 C−B。

`benchmark-results/extension-v1/pr4-matrix-development/attempt_0` 的五条原生容器轨迹已通过：Baseline/A 完整初始请求相同，B/C 初始请求相同；Skills 工具仅在 B/C/D，Memory 段及提议能力仅在 D；完成时可见检查仅在 C/D 实际执行，私有数据库不变，五个容器均移除。D 使用 fixture_operator 合成条目，不能算实际评测种子批准或机制收益。

两份独立教学输入已固定在 `benchmarks/extension_v1/teaching/`：倒数函数的零输入前置条件、映射缺失键的默认值。它们不是六题评测任务，不进入 90 行任务分母。原生禁网容器分别检查缺陷失败、参考解通过和实际 Loop 修复产物独立通过；仅模型提议后在运行外验证。教学经验不来自六题的答案、隐藏判据或既往反馈。

可审阅候选在 `benchmark-results/extension-v1/pr4-teaching-development/attempt_2/review.md`，其 JSON SHA-256 为 `fe38a84ea6b72b5acf485172e1321a04367080ad457c0afab509a771f9299e17`。两条经验为 validated；一条项目运行配置为 proposed，等待人工确认。三条均未批准、未提交，不会进入真实 D 组。首次缺少固定 test 能力的组装失败，以及随后 write_file 正确拒绝覆盖导致的脚本修复失败均完整保留；修正为 patch_file 后重新冻结教学响应，独立任务与判据未改。

种子审阅请求已经提出。只有收到明确批准，才绑定当前 revision/内容 hash、记录真实确认身份/UTC 时间，再按受控维护链完成批准、提交和副本冻结；不把等待时间或 fixture_operator 当成人工批准。

## 当前证据与未测项

当前 136 项 Memory 定向检查以及 11 项新增 Memory 配置检查覆盖 domain、真实 SQLite、来源 bytes/hash、维护链、生产组装和七策略。组件和模型脚本中的批准身份仍是 fixture_operator，不是用户批准的评测种子。Memory 关闭时不读取 Store，核心 Loop 相对 PR 3 封存仍无变化；当前存储修复的阶段冻结对照须重新固定。

原生 OS 开发探针保存在 `benchmark-results/extension-v1/pr4-native-development/`，不计为阶段验收：

- `attempt_1/check.json`：8 项通过。隔离进程知道宿主数据库精确路径并在同名 guest 路径创建伪造 approved 数据，宿主数据库字节和条目仍不变；绝对路径读取拒绝、正常工作区写入允许、模型批准工具不存在、容器移除。`attempt_0` 因夹具未建立 /workspace 映射而失败，原证据保留。
- `default_backend_2/check.json`：8 项通过。使用生产默认 Docker 组装，宿主数据库不可见且不变，合法工作区读写可用，原生架构与 1 GiB 限额正确；现场 Docker inspect 的 NetworkMode=none、无 IPv4 路由，实际 connect_ex 返回 ENETUNREACH。前两次以接口数量推断禁网的检查器错误记录均保留（Linux VM 的 down tunnel 伪设备不代表有路由），没有据此改动生产网络策略。
- `interrupt_timeout_1/check.json` 与 `interrupt_cancel_1/check.json`：各 5 项通过。实际启动独立 session 子进程，超时/取消停止整个容器，退出后移除，宿主数据库不变且维护守卫释放。`interrupt_timeout_0` 是检查器误读共享 ShellTool 的 result.timed_out 字段，保留为 checker_error，修正后另开目录。

以上均使用冻结 digest、原生 linux/arm64、1 CPU/1 GiB、禁网、无宿主挂载；合成配置确认仅用于开发安全夹具。没有真实模型调用，没有真实种子批准，M01–M04/E03 尚未整行签署。

真实模型对照的前置条件仍包括种子的人工确认、批准、提交及冻结；本阶段完整回归、OS 验收及证据归并状态以独立报告为准。禁止以测试通过数或合成操作者身份替代真实种子授权。

PR 4 完成前保持原 PR 3 封存；旧封存用 verify_seal.py 默认模式验证。真实模型队列仍为 0/90，留待所有能力、证据和种子固定后按原 720,000 Token 协议执行，不临时扩题或改变预算。

队列 freeze 可通过 `--seed-manifest /absolute/path/seed.json` 固定三条已提交种子、批准绑定及显式授权证据的全部 bytes；历史包可脱离原来源审计。每个 D 组运行复制同一私有数据库，不累积本轮新提议。归档中的协议也须与 manifest 一致，六题完整工作树及独立判据树使用原 M0 提交 hash 校验。

每条隔离运行采用冻结镜像、原生架构、1 CPU/1 GiB、禁网及无宿主挂载。Actor 停止后才用有界二进制 Docker archive 捕获产物；宿主只读取 tar 元数据/hash，不解包。独立容器再解包并装入原判据，避免验证器进入 Actor。原始 Python bytecode 仍保留在产物归档，独立验收忽略缓存。Node 冻结镜像没有 Python，因此文件端口有独立标准 Node worker，生产容器使用 Linux 目录描述符锚定路径；不安装运行时或替换镜像。该 worker 只承接固定矩阵实际注册的文件与 Skills 能力，未实现 find_symbol。

用量评分遵守响应显式 usage_known=False：保留数字下界与已知超量，但完整消耗保持未知，并停止真实队列；未知调用不进入完整缓存命中率分母。

完整队列开发演练的 attempt_0 在首行独立解包停止：Docker Desktop 保留宿主 UID/0600，使去除全部 capabilities 的验收进程无法读取归档。原 Trace、用量及产物保留；用原归档单独复查，确认仅在隔离容器内由原数字 UID 设置副本只读权限后，解包和原独立判据均通过。宿主归档权限和容器能力不变，新版本使用全新目录演练，不覆盖失败证据。

`examples/project_memory_demo.py` 是可直接运行的可选 Memory 示例：

```bash
uv run python examples/project_memory_demo.py
```

它先检查关闭 Memory 不创建数据库，再在显式私有目录中保存 proposed，并在 run 退出后使用操作者入口审阅。未验证经验保持未批准，不进入 Context；不使用进程能力或真实模型，不代替三个评测种子的审批。

统一队列的 `pr4-native-rehearsal/attempt_1` 开发演练已完成全部 90 行，独立判据 90/90 通过，180 个容器全部移除；18 个 D 组种子副本字节不变，18 对 Baseline/A 完整模型请求相同。脚本已知消耗 132,030 Token，用量完整，真实 API 调用为零。该历史演练源码为 `c27ecc3e779c837b3f14e2a3b5efa9b27976f5fa52bc78edfea50e807394eb8e`；后续仅修复生成报告导致冻结身份循环的问题，并补充本文和示例，最终出关仍按最终冻结源码复核。它证明执行接线与确定性回归，不证明机制收益或用户批准了合成种子。

freeze 明确排除 `docs/results/extension-pr4-*.{json,md}` 和 `extension-m5-*.{json,md}` 派生输出，避免报告自包含源码 hash；这些报告须另入阶段证据索引。历史阶段输入、普通文档、协议和判据仍参与源码身份核对。公开 CLI 回归验证发布报告后源码 check 不变，真实源码或冻结任务变化仍拒绝。

常规完整门禁与冻结归档解压门禁均为 958/958（上述演练版本）。曾把可选 Trace 观察器扩展到全仓，导致 13 项使用简化 EventSink 或虚拟时钟的测试受到采集副作用影响；原失败日志保留。按既定契约模块采集的 525 项全过，常规全仓测试也全过，不将采集器冲突改写成生产缺陷或删除原失败。新增生成输出回归后的当前收集规模为 960，最终门禁待当前源码运行确认。

## 目录描述符存储修复（2026-10-30）

SQLite 文件格式及 schema 1 保持兼容；停止旧写入实例后可直接读入，内容不做自动迁移。数据库上限 8 MiB，serialize/deserialize 与临时存储均在内存中进行。已授权目录由设备/inode 绑定，所有物理读写使用同一目录描述符；同根目录 Store 用目录 advisory lock 协调，不创建锁文件。外部 SQLite writer 和旧 adapter 不参与该锁，不能并行维护；未结束的 journal/WAL/SHM 拒绝接入而不删除。

该 adapter 要求 POSIX dir_fd/O_NOFOLLOW/目录锁以及 Python SQLite 的 serialize/deserialize 支持，缺少能力时失败关闭。它不提供防御恶意同 UID 宿主程序的数据库内容完整性承诺；Actor 执行世界仍须按原契约隔离。

新增 15 项回归覆盖四条读写路径的目录/末端符号链接替换、原子发布瞬间替换、跨进程争用与完整提交、三种未结束 sidecar 及容量上限。先保存路径读取与原子发布两组失败记录，再实现定向修复；所有 29 项 SQLite 检查目前通过，完整门禁和原生阶段探针须按本次最终源码重新固定。旧报告的“路径竞争未验证”只描述其历史版本，不反向改写。

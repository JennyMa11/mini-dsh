# Skills 与 Context：PR 2 开发记录

用户恢复 M0–M5 目标后进入 PR 2。PR 1 原封存证据保持不变；范围依据 [主计划](extension-roadmap.md)，本阶段状态以 [PR 2 独立报告](results/extension-pr2-2026-10-29.md) 为准。

已实现 `domain/skills.py` 的纯数据契约：UTF-8 SKILL.md 解析、元数据快照和单 run 激活状态。解析器不读盘、不执行命令、不展开引用，也不决定权限。`SkillCatalog` 只保留 name、description、工作区相对 source path 与原始文件字节 SHA-256，不保留正文；`SkillRun` 在显式加载后保存正文。目录与读取现已接到受控文件操作及工具层；完整 Context/Loop 接入和 OS 验收仍待完成，不能把组件路径检查当成 OS 隔离。

文件语法固定为首行和闭合行 `---`，中间恰含 name/description 两个唯一键，允许空行和整行注释。字符串支持普通单行文本、JSON 双引号字符串，以及用双单引号转义的单引号字符串；不执行 YAML 标签、锚点、集合或块标量。数字、布尔值和 null 等须显式加引号才能作为字符串，注释不能代替描述。允许 UTF-8 BOM 和 CRLF，hash 始终取原始字节。name 使用 snake_case，description 最多 256 字符，文件最多 32 KiB（含头部），快照最多 32 项，同一 run 最多激活 3 个不同技能。

元数据按源路径排序，重复名称和重复源都拒绝。文件内容或头部变化导致源 hash 改变，旧 run 拒绝激活；不会静默替换目录快照或继承另一 run 的激活状态。相同技能再次激活不占新名额，实际加载仍须重新校验来源和权限，不能据此绕过后续读取消耗。

人工示例位于 `examples/skills/{bug_fix,test_debug,code_review}/SKILL.md`。它们是任务资料，存在于目录中不会使 Runtime 自动发现、安装或执行。后续只扫描显式配置的工作区内根目录，并以 load_skill 工具按需激活。

当前 25 项定向检查通过，含恶意/畸形头、大小边界、非法 Unicode、快照改变与激活限制，先记录红灯再实现。三个示例独立解析通过。开发日志和源码身份见 `benchmark-results/extension-v1/pr2-parser-checkpoint/`。这些证据仅覆盖 S02 的部分纯组件要求，不能把 S01/S02/S03/S04 或 C02–C04 整行签为通过。

接下来的必需工作：通过原 Tool Runtime、Permission 和 FileBackend 扫描/加载并计账；显式 run 身份绑定；模型初始只见元数据；Context 组合及七分支来源/裁剪/配对/全局请求上限；真实符号链接、文件替换与授权探针；阶段冻结复验和报告。Skills 之外的 Verification、Memory 不在本阶段实现。

## 受控文件与工具检查点

FileBackend 增加封闭的 skill_scan / skill_read 操作，本地适配和容器内标准库 worker 使用同一实现。扫描只接受显式根目录，保留原始 UTF-8 字节的 hash，最多读取 32 个、每个 32 KiB 的 SKILL.md；越界或敏感链接、循环、重复来源及不完整输入明确失败，不返回部分目录。内部链接的实际来源规范为工作区相对路径，加载按冻结 hash 校验。普通文件操作的旧调用形式保持兼容；未显式绑定工作区的本地 backend 不获得新的扫描能力。

Tool Runtime 增加可选 RunScopedTool.execute_in_run 接口，在 Schema 和授权之后注入真实 run_id/step_id。旧工具继续使用 execute。SkillService 按 run 保存不可变目录及已激活正文；discover_skills 的配置根不在模型 Schema 中，load_skill 只接受 name。额外 run_id、path、roots 等字段在 Schema 阶段拒绝。工具均需 fs.read；服务本身不决定权限或增加额度，之后仍须由 PreparedContext 的受控执行入口计入总预算。

发现结果只含元数据。加载结果只确认 name/path/hash/loaded，正文保留在当前 run 的只读视图，等待 Context 在下次请求前注入一次。run 关闭使在途发现/加载结果失效，不能重新开启状态；未知运行、文件变化、超过三项激活和 malformed backend 结果均不会产生部分激活。

本批新增 14 项真实临时文件检查和 9 项工具/运行状态检查；连同前批解析检查共 48 项通过。容器传输沿用现有 FileBackend，当前尚未用本阶段真实容器探针签署 S03；不能把本地共享 worker 测试等同于 OS 隔离验收。记录保存在 benchmark-results/extension-v1/pr2-file-tool-checkpoint/。

待完成：显式 Skills 配置与两条组装入口、运行结束自动释放、受控扫描计账、load_skill 的真实 ModelRequest 轨迹，以及统一 Context 来源/角色/有序段与预算。启用 Skills 时还须防止自动 Repo Retrieval 在 load_skill 之前读取 Skill 正文，保留原始 Session，确保正文只组装一次。S01–S04/C02–C04 均尚未整行验收；未实现 Verification 或 Memory。

## Context 组装与显式启用

PR 2 实现已接入 `application/context_assembly.py`；前面的“待完成”列表是历史检查点；当前出关状态以阶段独立报告为准。配置默认关闭，Python embedding 使用 `RuntimeOptions(skill_roots=("examples/skills",))`，配置文件可写：

```toml
[minidsh]
skill_roots = ["examples/skills"]
```

路径相对于当前工作区，只扫描这些根。若显式设置 enabled_tools，须包含 load_skill，否则组装明确失败；Work 自动生成的工具集合会在组装时包含该工具。discover_skills 仅供受控 Context 准备使用，不出现在模型工具 Schema 中。三个人工示例可直接用于此配置。

统一组装复用 full/sliding/summary/retrieval/retrieval_summary/symbol 以及 budget；所有分支记录来源，关闭时保持底层策略消息恒等。启用时顺序是系统约束、项目约束、目录元数据、已选正文、任务及关键状态、策略选中的历史和证据。Skill 正文、摘要中的不可信历史和仓库原文以 User 资料角色出现，不能覆盖 System/AGENTS 或增加权限。状态保留只识别 System 标签，工具恰好同名不会被抽出因果轮；其他未命名系统约束也保留。本地和容器的列表均先规范为真实相对来源，自动检索排除所有 SKILL.md（含内部链接的别名），避免先于显式选择将正文带入请求。显式读取普通文件的原有权限规则不变。

每段记录 source_id、版本、source_sha256（Skill 的原始文件 hash）、渲染 sha256、信任、顺序、Token 估算和省略原因。逐段估算用于解释来源；不能把它们相加代替最终请求计数。原始 Session 不接受目录或正文注入。加载工具仅返回确认，多次加载仍逐次读盘、核对和计账，Context 中只保留一个当前激活副本。BudgetContext 复用原算法为保护段留出预算；最终请求经过全部 Hook 后再次检查上限，含可用的模型工具 Schema 计数。估算不是 Provider 实际 Token 的保证，实际用量仍按响应记录。

发现和加载计入原工具/墙钟预算；权限拒绝、未知运行、畸形目录、文件变化、超限和取消不会产生部分 Context。运行终态释放快照，配置式和显式 Plugin 两条托管入口均释放对应状态。本地新增安全打开需要 dir_fd/O_NOFOLLOW，缺少能力时明确拒绝；跨平台扩展路径和原因见 [ADR 0024](adr/0024-controlled-skill-context.md)。本地路径检查仍不替代 OS 隔离。

当前 Skills/Context 定向检查共 99 项通过。新增证据包括七分支实际请求、渐进正文与单次计数、跨 run 隔离、长历史裁剪/摘要/受控检索、来源与因果配对、未改写 Hook 的全局上限、工具 Schema 预算、取消发现计账、两条托管入口、恶意命令/引用不自动执行，以及路径检查后文件/目录替换的回归。阶段判据冻结在 `benchmarks/extension_v1/pr2_cases.json`；真实容器探针、六题离线历史对照、完整归档复验与出关结论统一记录在 [PR 2 独立报告](results/extension-pr2-2026-10-29.md)，不能仅据组件测试把阶段签为通过。

# ADR 0031：显式分层检查与独立证据输入解码

- 状态：Accepted
- 日期：2026-11-05

## 背景

用户要求梳理项目结构并检测分层，随后选择先稳定分层与检查。当前物理目录包含核心
编排、运行时机制、能力扩展、交互入口及评测；仅按目录名建立单一数字层级会误把
runtime/skills 对工具契约的使用当成核心 Runtime 与 Tools 的循环。

检查另发现生产 JsonMemoryEvidenceSource 直接导入 evaluation.harness_metrics，仅为
复用 ScoringEvidence 输入模型。这样会把计分算法及其依赖带进生产 Memory 来源核验。

## 决策

1. 保持现有入口与物理目录，建立按实际职责划分的显式导入允许列表。区分 Runtime
   核心与 Skills/Memory/Verification 扩展；Composition 和 Presentation 是外层。
2. 使用标准库 AST 检查绝对、相对、局部和类型注解分支的显式导入，检测模块强连通组。
   新的未分类顶层源码必须明确归属。Domain、Ports、Application 禁止第三方依赖。
3. 将 ScoringEvidence、ScoringBudget、Checks 原样提取到 adapters/scoring_evidence.py，
   作为独立验证 sidecar 的输入解码实现。Memory reader 与 Evaluation 共用它；旧计分
   模块继续显式导出原有名称。格式、校验、from_file 和计分口径保持不变。
4. Evaluation 只对该精确 adapter 模块放行，不允许任意 adapter 导入；生产 adapters
   不得反向导入 Evaluation 或 Benchmark。
5. 接入 pytest 与 CI，记录可再生成的源码/检查器 hash 报告，并以 docs/architecture.md
   作为目录职责、调用链与维护清单的导航。

## 替代方案

- 一次性移动所有目录：会扰动公开导入路径、冻结脚本和证据，本次不采用。
- 将 Pydantic 输入模型放入 Domain：混合纯领域规则与外部输入校验，不采用。
- 在 Memory reader 复制一份宽松输入模型：可能改变拒绝行为并产生 Schema 漂移，不采用。
- 为现有反向依赖添加永久白名单：没有消除计分实现与生产路径的耦合，不采用。
- 新建通用 contracts 包：当前只有一个共享输入解码需求，无须扩大模块体系。

## 后果与限制

新增能力是开发期结构检查，不改变 Agent 的可见工具、预算、权限、Context 算法或
Memory 状态机；不推进 Extension 路线图的功能阶段，也不重签冻结评测。

AST 检查只证明列出的显式导入规则，不能覆盖动态加载、隐式 package 初始化链、对象
关系或真实安全效果。行数热点不等于设计缺陷。后续改变规则须同时更新本决策的后继
记录、结构说明与契约测试，不能为通过检查任意放宽核心依赖方向。

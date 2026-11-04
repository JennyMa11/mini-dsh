# ADR 0023：有界扩展升级与完成决策

- 状态：Proposed（执行规划已记录；实现须逐阶段验收，不表示现有代码已经符合）
- 执行与验收：[扩展升级主计划 v1](../extension-roadmap.md)

## 背景

当前已有 PluginManager、ServiceRegistry、HookRegistry、AgentFactory 和 Session 事实层。
重新引入 ExtensionRegistry 会分裂生命周期与权限边界；现有 finishing 回调只能观察终态，
不能承载验证失败后继续修复。若直接在结束回调重开 run，则会重置预算并破坏事实归属。

## 决策

扩展是插件提供的可选执行能力，统一使用现有插件生命周期和 Hook 服务。核心只消费
显式端口，新增通用 FinishProposal 与 allow/continue/fail 决策；具体验证命令留在可选能力层。
所有扩展 I/O 经受控、可计账的 Tool Runtime 调用入口，不能凭 Hook 信任绕过授权。
新接口按显式注册顺序执行，旧 priority/owner 排序和旧 finishing 语义通过版本化 adapter 保留。
Session 事实不被上下文、技能和记忆改写，原始工具结果与观察扩展错误分别记录。

范围冻结为四个功能 PR：扩展契约、Skills/Context、Verification、轻量 Memory。
Memory 使用可替换的 SQLite adapter，写入须经过提议、验证、批准、提交；无自动批准与跨项目共享。
运行期可见验证与独立验收分开；finished 不代表任务成功。功能正确性与配对实验收益分开报告。

## 替代方案

- 新建平行 Extension Runtime：调用方便，但重复注册、清理和权限逻辑，拒绝。
- 每个能力单独重写 Loop：局部实现容易，但预算与终态一致性不可维护，拒绝。
- 只用 EventBus 监听结束：适合观察，无法可靠拒绝候选完成或返回有限修复，拒绝。
- 一次性迁移全部旧 Hook：接口更整齐，但会改变已有扩展顺序和错误行为，采用兼容迁移。

## 后果与迁移

PR 1 必须先建立通用控制契约，因此工作量高于“增加六个回调”；后续能力不得再将
测试命令、技能解析或 Memory 存储细节放入 Loop。保持现有 composition.py 入口，不强制目录迁移。
旧 API 保持可用，新能力显式启用；本轮不删除旧接口。既有 GUI/协作等外围能力只回归兼容。
验收矩阵、预算和停止条件以主计划为准。扩大边界需显式范围变更，不能自动衍生下一轮功能。

# ADR 0024：按运行加载 Skills 与 Context 组合

- 状态：Accepted with limitations（实现完成；阶段出关以 PR 2 独立报告为准）
- 范围：[扩展主计划](../extension-roadmap.md) PR 2；不增加 Verification 或 Memory。

ContextAssembly 组合现有 ContextManager 和 PreparedContextManager，不复制策略，也不持有文件系统句柄。只读 SkillView 提供当前 run 的目录快照与显式加载正文；发现和加载由 ToolRuntime 完成 Schema、授权、超时、Observation 和事件。准备阶段在调用前计账，取消时仍保留尝试数和耗时。模型只能用 name 选择技能，run_id、根目录及来源 hash 由运行时绑定。

工具增加可选 RunScopedTool 接口，旧 execute 协议保持。发现仅返回元数据；加载只返回确认，正文作为低信任 User 段进入下一次派生请求一次，不进入 Session 事实。启用时，自动检索排除 SKILL.md，摘要与仓库原文以 User 资料角色组装；关闭时保持底层策略的消息输出。来源记录补充 ID、源 hash、渲染 hash、信任级别、顺序、估算与省略原因。BudgetContext 使用可替换计数器为新增保护段预留空间，最终请求在全部 Hook 后再次检查上限。

真实文件替换测试发现 realpath 检查后仍可能被重新定向。因此 Skills 的基础设施读取使用逐级 dir_fd + O_NOFOLLOW，拒绝中间目录和最终文件的链接替换，并限量读取普通文件。允许的内部链接先规范化成真实的工作区相对来源，再使用该打开方式。缺少这些能力的平台明确拒绝 Skills 读取，不降级成不安全打开；默认关闭 Skills 的跨平台 Runtime 不受影响。后续支持其他平台应增加等价 adapter 与实测边界，不以 Prompt 或事后 hash 检查替代防逃逸。

替代方案：只在读取前 resolve 不能关闭替换窗口；只在读取后比较 hash 可能已经读取越界内容；将全部 Skills 内容塞入工具结果会重复消耗历史与前缀 Token。以上方案均不采用。

本地路径约束不等于 OS Sandbox。安全验收仍使用原生、禁网、固定镜像的容器，单独观察文件与进程副作用。运行结束和托管关闭释放当前服务状态；不会在清理阶段读盘、执行命令或重开 Loop。

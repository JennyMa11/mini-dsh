# ADR 0012：兼顾 DSH 架构与 Harness 实验

- 状态：Accepted with limitations

## 背景

项目需要同时研究 Harness 对成功率、Token、成本和延迟的影响，并更忠实地实现
DSH 的 Session 事实源、可替换 Loop 与外部工具接入。此前 Loop 直接维护消息列表，
插件组装不能替换 Loop，本地工具之外没有 MCP adapter。

## 决策

引入 `SessionJournal` / `SessionLog` 端口和带版本、序号、UTC 时间、步骤与消息的
`SessionMessageEvent`。Loop 先提交消息事实，再通过 `messages()` 投影构建 Context。
Context 压缩和检索不回写日志；请求中实际使用的派生视图继续记录到原有 Trace。

默认本地组装使用 `.minidsh/session_events/<run_id>.jsonl`。独占创建，追加写入并
fsync；已有日志不覆盖，重新打开的句柄只读。损坏或不完整尾部明确报错，不自动
修剪或推断执行结果。内存实现用于嵌入和实验，两者采用相同凭据脱敏规则。
事件先脱敏后进入模型历史，因此磁盘事实和在线投影一致。

保留终态 JSON 快照、旧 Session 读取 API 和 `prior_history`。新运行的快照历史从
事件日志生成；恢复时日志优先，旧运行没有日志时继续读取快照。运行 ID 仍是每轮
日志身份，上一轮历史作为新轮初始事实导入；尚未引入官方的跨轮 Session ID、分叉
与格式迁移体系。Trace 继续记录工具、权限、模型用量、Context 和终态等完整执行事实。

新增 `AgentFactory` 和 `AgentDependencies`：Loop 工厂收到模型、Context、工具、
事件、Session、Journal、预算及实验选项。`plugin_runtime` 默认挂载
`AgentLoopPlugin`，也可选择外部工厂服务。同步 `build_local_runtime` 保留为兼容的
显式组装入口，使用同一工厂契约；不引入隐式插件发现或全局服务定位器。

MCP 通过 `McpPlugin` 提供 `ToolBundle`，发现的工具进入原有 ToolRuntime。
JSON Schema 在执行前验证；拒绝外部 Schema 引用。权限默认拒绝，必须显式授予
`mcp.<server_id>`，远端只读声明不自动授权。未知远端行为按有副作用处理，不自动重试。
结果统一限长、offload、计时、产出 Observation，并进入同一 Trace。

首个实际 transport 使用官方 Python MCP SDK 的 Streamable HTTP。连接必须由
可信组装代码显式配置，并通过 host / TCP / port 授权。关闭代理环境继承与重定向，
不提供隐式凭据、子进程启动、服务端模型采样授权或 roots 授权。启动超时、目录上限
与分页上限有界，插件退出或启动失败时释放连接。SDK 与 JSON Schema 为可选运行依赖。

## 替代方案

- 直接嵌入官方 Node Runtime：能获得 Cordis 兼容性，但改变语言基线和实验控制范围。
- 把 EventBus 订阅器当作事实提交器：订阅失败被隔离，不能保证模型读取前事实已提交。
- 用 MCP 服务绕过 ToolRuntime：会失去统一授权、预算、错误反馈和评测口径。
- 全量移植 DSH 产品功能：引入大量与当前实验目的无关的机制，难以固定基线。

## 后果与迁移路径

实验指标、终态语义和 Context 接口保持兼容；新日志提交增加的耗时归属 Harness。
历史报告是旧实现的实测记录，不能当成新实现的性能证明。比较策略时固定 Journal、
Loop 工厂、MCP 服务版本与工具目录，只改变一个实验变量。

当前是 DSH 思想的 Python 独立实现，不兼容 Cordis/npm 插件。
HTTP 授权限制客户端连接目标，不限制远端工具内部的网络、文件或进程行为；远端服务
必须由部署方信任和隔离。后续 stdio、跨轮 Session ID、插件配置树及日志迁移需另立契约。

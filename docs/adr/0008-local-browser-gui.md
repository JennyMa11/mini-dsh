# ADR 0008: 本地浏览器 GUI 展示层

- 状态：接受
- 日期：2026-09-27

## 决策

GUI 位于独立的 `minidsh.gui` presentation 包，复用 composition、AgentLoop、
Session、Tool Runtime 和 Event Bus，不进入 domain/application/runtime 核心层。
维持单 Agent、单工作区、单进程；同一 asyncio loop 处理本地 HTTP 和 Agent task。
采用标准库本地 HTTP 服务与无构建步骤的 HTML/CSS/JavaScript，不新增运行依赖。

服务只监听 `127.0.0.1`，默认使用系统分配端口。随机会话凭证通过 URL fragment
交给浏览器，只在 API 请求头中携带；校验 Host、Origin 与凭证，不开放 CORS。
请求、响应展示和事件缓存都有大小上限。未信任内容使用 textContent 渲染。
GUI 不接收密钥、不切换工作区、不提升权限；权限、模型、Context 和预算沿用 CLI
配置。所有工作区操作（包括 Diff）继续通过 Tool Runtime。

每轮订阅现有脱敏 Trace Writer，落盘原始事件；浏览器轮询的是有界派生视图，
截断明确显示，不改写 Trace。连续任务使用 prior_history，恢复复用 Session adapter。
同一时刻只能执行一个任务或 Diff，执行期间禁止清空或恢复；取消等待 Loop 完成
cancelled 状态保存与工具清理，随后恢复已保存历史。关闭服务也取消正在执行的任务。

## 替代方案与迁移路径

不引入桌面框架或前端构建链，降低安装与跨平台成本。当前展示按事件更新，
没有逐 Token streaming；以后可用 SSE 替换轮询而不改变核心接口。
不支持远程服务或多用户认证；如需远程访问，应先增加独立安全设计与 ADR。
底层阻塞 HTTP adapter 的已发送请求可能仍等待超时，沿用 ADR 0007 的取消限制。

## 验证

先编写展示层契约测试，覆盖连续对话、只读权限、运行互斥、取消、恢复与脱敏。
本地 HTTP integration 验证认证、跨站拒绝、输入边界和实际 API 对话。

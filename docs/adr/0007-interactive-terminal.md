# ADR 0007: 在现有 Harness 上提供交互式终端会话

- 状态：接受
- 日期：2026-09-26

## 决策

交互层属于 CLI presentation，使用标准库输入输出和一个持续的 asyncio Runner。
无参数启动默认进入 `chat`，使用现有 DeepSeek profile；已有 `run`、Benchmark、
消融和 Trace 命令继续提供可复现的批处理入口。核心层仍是单 Agent、单工作区、
单进程，没有引入图形界面、厂商 SDK 或新的终端框架。

AgentLoop 增加显式注入的不可变 `prior_history`。每轮具有新的 Run ID、独立预算、
Step ID、终态及 Trace，历史以原始 Message 继续传入相同 Context 接口。
续接时保留当前系统约束，且不重复添加上一轮初始系统消息。
取消或预算耗尽可能留下未配对的工具调用，续接视图为其补充结构化
`interrupted` Observation，标明执行结果未知；不会重试调用，也不会改写原始 Trace。
补充行为记录在 `conversation_restored` 事件中。

交互层订阅现有 Event Bus 展示步骤进度；所有工作区读取、写入、命令和 Git diff
继续通过 Tool Runtime 和 Permission System。权限默认只读，由已有配置显式授予。
对话落盘复用 Session adapter，恢复时保留 DeepSeek reasoning 字段，落盘前统一脱敏。
Prompt 输入历史只保存在当前进程，不额外写入用户主目录。

交互模式不要求事先配置价目，未声明时 UI 明确显示成本不可用；批处理评测的
价目要求不变。不把不可用成本显示成免费，也不把模型自报完成视为测试成功。

## 替代方案与边界

不在 CLI 里实现第二套 Agent Loop：它会造成权限、预算和事件链路分叉。
不把历史拼成单个用户 Prompt：工具调用与结果必须保留 provider 协议结构。
首版采用行式输入，后续可替换为终端 UI adapter；不将其依赖放入 domain/application。
目前模型输出按步骤展示，没有逐 Token streaming。

Runner 在 Ctrl+C 时取消当前 Agent task，Loop 保存 cancelled 状态，本地命令 runner
负责释放进程组。现有 HTTP adapter 使用线程执行阻塞 I/O，Python 不能强制取消已发送
请求；因此退出可能等待 HTTP 超时。未来通过可取消的异步 transport 解决，Model port
和交互层不必绑定具体网络库。

## 验证

契约测试覆盖连续对话、独立预算、未完成调用补全、Session reasoning 恢复与脱敏。
CLI 测试覆盖无凭据启动、取消后继续、清空、多行输入、会话恢复及默认只读。
临时工作区 e2e 使用脚本模型读取文件、Patch 修复、运行真实测试并继续追问。

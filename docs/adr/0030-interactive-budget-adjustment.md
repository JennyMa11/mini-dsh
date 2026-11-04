# ADR 0030：交互会话内调整运行预算

- 状态：Accepted with limitations
- 日期：2026-11-04

## 背景

用户希望最大步数、Token、工具调用次数和墙钟时间这四类预算在启动项目后可以直接修改。
此前只能在启动前通过 CLI 参数、环境变量或 `minidsh.toml` 设置，交互会话内固定不变。

## 决策

新增 `/budget` 命令：

- 无参数显示 `max_steps`、`max_tokens`、`max_tool_calls`、`max_duration_seconds`。
- `/budget KEY VALUE` 修改，键为 `steps`、`tokens`、`tools`（或 `tool-calls`）、`seconds`；
  值必须为正整数或正浮点数。
- 修改只更新当前进程的预算状态，不写入 `minidsh.toml`，重启后恢复启动配置。
- 修改后关闭当前 Runtime，从下一轮任务开始用新预算重建 Runtime；会话历史、Session ID
  和已有 Trace 保留，不重放工具调用。
- 显式 CLI 参数、环境变量和配置文件仍是启动时的预算来源；冻结 Benchmark 不使用
  交互命令，历史结果不受影响。

实现复用现有 `AgentBudget`、Runtime 组合和 Session 持久化，不修改 Agent Loop 的预算
语义，也不提供运行中修改当前任务预算的能力。

## 替代方案

- 修改全局默认值：影响所有运行和测试，且不满足“启动后修改”。
- 只允许启动参数：交互会话内无法调整，未满足用户要求。
- 直接修改 Agent Loop 内部预算对象：运行中语义不清，缺少受控重建与清理。

## 后果与边界

预算修改从下一轮生效，当前运行不可变。关闭并重建 Runtime 不改变工作区文件、对话历史
或 Trace。预算值不持久化，重启后回到启动配置。四类预算仍是硬限制，不会因交互修改
绕过 Trace、审批或安全策略。

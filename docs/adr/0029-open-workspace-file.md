# ADR 0029：打开工作区文件

- 状态：Accepted with limitations
- 日期：2026-11-04

## 背景

用户要求 agent 直接打开 `index.html`。work 的 shell 白名单只包含开发命令，
没有 macOS `open` 或 Linux `xdg-open`；也没有专用工具。模型尝试用
`python3 -m http.server` 预览，但 shell 是同步阻塞执行且受超时限制，
服务器无法常驻，而且静态页面本来不需要服务器。

同一次会话还暴露了观测问题：进程超时后结果包含 `timed_out=true`，
但工具 Observation 仍标记为 `succeeded`，交互层显示与实际结果矛盾。

## 决策

新增内置工具 `open_file`：

- 输入只有工作区相对 `path`；必须存在，经过路径规范化、符号链接和工作区
  归属校验，并拒绝 `.env`、`.minidsh` 等敏感文件。
- 仅在 `sandbox_backend=local` 时注册。macOS 使用 `open`，Linux 使用
  `xdg-open`；没有宿主打开器的平台不注册该工具。
- 使用独立的 `LocalCommandRunner`，其策略只允许打开器本身，不把
  `open`/`xdg-open` 加入模型可调用的 shell 白名单。
- 工具声明 `side_effects=True` 和 `approval_required=True`，需要
  `process.exec`；因此随 `allow_shell` 授予，交互模式每次打开仍需审批。
- 失败返回结构化错误：`sandbox_violation`、`file_not_found`、
  `open_failed`、`open_timeout`。

同时修正超时观测：结果字典中 `timed_out=true` 的工具 Observation 状态改为
`ToolStatus.TIMED_OUT`；交互层把测试超时显示为 `timed out`，而不是
`failed (exit -9)` 或 `succeeded`。

## 替代方案

- 把 `open`/`xdg-open` 加入 shell 白名单：改动更小，但 shell 只校验可执行
  文件，不校验参数，模型可以打开任意路径或 URL；专用工具能限制在工作区。
- 增加后台进程或开发服务器能力：能解决常驻服务器，但不能替代宿主 UI
  打开动作，且需要进程句柄、日志、停止和清理，范围更大。
- 在工具中直接调用 `subprocess`：绕过 Tool Runtime、权限和审批，不采用。

## 后果与边界

打开动作发生在宿主桌面。Docker 后端不注册该工具，因为容器内无法可靠控制
宿主应用。文件关联、无 GUI 会话、SSH 远程环境和 Windows 尚未验证。
每次打开需要审批；敏感文件与越界路径仍被拒绝。

超时状态变更会让历史 Trace 与新 Trace 的 `tool_call_completed.status` 含义
不同：旧 Trace 可能把超时记录为 `succeeded`，新 Trace 记录为 `timed_out`。
这是更准确的观测语义，不回写历史 Trace。

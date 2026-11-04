# ADR 0028：work 默认注册 shell 与常用命令白名单

- 状态：Accepted with limitations
- 日期：2026-11-04
- 取代：ADR 0019 中“work 默认不注册通用 shell”的默认值部分

## 背景

用户要求裸启动 `mini-dsh`（等价于 `work`）即可使用 shell，不再每次手动传入
`--allow-shell --allow-command`。此前 work 只注册读写与固定 test/build，通用 shell
必须显式授权。默认拒绝仍是全局安全基线；本次是用户明确要求放宽 work 入口的默认能力。

## 决策

`work`/裸启动默认 `allow_shell=true`，注册 `shell` 工具，并提供常用开发命令白名单：
`git`、`uv`、`python`、`python3`、`pytest`、`ruff`、`mypy`。命令仍由 CommandPolicy
按可执行文件检查，以参数数组执行，不拼接 shell 字符串。

- 显式 `--no-allow-shell` 可关闭；关闭后不授予 `process.exec`，也不注册 shell。
- 配置、环境或 CLI 显式给出的 `allowed_commands` 优先于默认白名单；需要其他程序时
  用 `--allow-command` 列出。显式列表不会自动并入默认白名单。
- `chat`、`run`、GUI、评测与冻结配置保持原有默认值。
- 交互式 work 默认 `approval_mode=ask`；shell 有副作用且 `approval_required` 为真，
  因此每次调用仍要终端审批。`--approval-mode configured` 只用于用户显式选择或离线测试。

## 替代方案

- 保持默认拒绝：仍然需要启动参数，未满足用户“任意目录裸启动即可用 shell”的要求。
- 默认允许任意程序：扩大副作用范围且白名单失去意义；选择保留可执行文件白名单。
- 只提供工作区配置文件：每个新目录都要重复配置，不能覆盖“任意路径启动”的目标。

## 后果与边界

work 入口默认授予 `process.exec`，permission 层不再拦截 shell；剩余边界是可执行文件
白名单、逐次审批、工具超时、输出上限和路径归属检查。本地后端仍在宿主机器执行，不是
操作系统级隔离；不可信代码应改用 Docker 后端。

已接受但尚未验证：真实用户在不同终端中的审批体验、白名单是否覆盖日常命令，以及
长命令与交互式程序的输出表现。冻结 Benchmark 不使用 work 预设，历史结果不受影响。
现有依赖“work 无 shell”的使用者需显式传入 `--no-allow-shell`。

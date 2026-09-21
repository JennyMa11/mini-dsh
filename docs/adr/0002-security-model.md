# ADR 0002：默认拒绝的本地安全边界

- 状态：Accepted with limitations
- 日期：2026-09-21

## 决策

- 文件工具只接受工作区相对路径，使用真实路径检查阻止 `..` 和符号链接逃逸。
- 写入使用同目录临时文件与原子替换；优先提供精确文本 Patch。
- `.env`、私钥、常见 credential 文件和 `.ssh` 内容不可通过 Read/Search 工具进入模型 Context。
- Shell 只接受 argv 数组，不调用 Shell；denylist 优先于显式 allowlist。
- 子进程仅收到环境变量 allowlist，并在超时后清理进程组。
- 网络工具默认拒绝；高权限工具只有在每次运行显式授权后才注册并授予能力。
- Trace 不记录 Prompt/工具正文，并对 Secret 字段及 Bearer token 二次脱敏。

## Hardened adapter

`DockerCommandRunner` 提供可选的进程硬隔离：容器根文件系统只读，仅将 workspace 挂载为可写，使用临时 `/tmp`，丢弃全部 Linux capabilities，启用 `no-new-privileges`，设置 PID/内存/CPU 上限，并默认使用 `--network none`。镜像由每次运行显式选择，工具协议与 Agent Loop 无需改变。

## 当前限制

`LocalCommandRunner` 是工作区、命令和环境策略层，不是容器或内核级隔离。允许执行 Python、编译器或其他通用解释器时，它们理论上仍可直接发起系统调用或网络访问。因此，不可信模型与不可信仓库应在外层容器/VM 中运行，并关闭网络；本地模式不能宣称为恶意代码提供硬安全边界。

Docker backend 依赖宿主机正确安装并配置 Docker，其镜像供应链也必须由部署方固定 digest 和审计。本地模式仍不会默认注册 Shell 工具；执行不可信任务应使用 `--sandbox-backend docker`，且不要启用 `--allow-network`。

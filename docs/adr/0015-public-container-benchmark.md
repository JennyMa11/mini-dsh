# ADR 0015: 公开任务的容器执行与独立验证

状态：Accepted with limitations

## 背景

本地 fixture runner 在宿主机复制工作区并执行验证命令。公开 SWE-bench Pro V2
任务依赖固定 Linux/amd64 镜像和留出测试，不能复用该宿主机执行路径。

## 决策

- 数据集桥接放在 `benchmark/public_swe.py`，通用持久容器适配器放在
  `infrastructure/containers.py`。核心 Loop、Model 和 Context 接口不绑定数据集。
- 模型留在宿主进程。Agent 仅通过 Tool Runtime 的 `shell`（argv 数组）和 `git`
  工具操作独立容器，无宿主目录、Docker socket 或 Secret 挂载。
- 容器固定 Linux/amd64 和镜像 digest，断网，禁用 capabilities 与提权，限制资源。
  Deadline 与取消终止整个容器；失败清理不静默吞掉。模型 argv 不拼入 shell 字符串。
- 准备阶段固定上游提交并校验完整 Git task tree（包括判据与参考解），
  只有题目规格提供给模型。先运行参考 Patch/空 Patch 门禁，再构造模型 adapter。
- 镜像下载可显式指定国内 TLS registry 代理。先解析上游 Linux/amd64 digest，
  按相同 digest 拉取并验证架构与 RepoDigests；代理不可用时回退官方源。
  该入口只影响宿主准备阶段，不向 Agent 或验证器开放网络。
- Agent Patch 相对固定初始提交捕获，包含新文件。无论 Loop 正常结束或预算耗尽，
  可捕获的 Patch 都交给全新镜像和未修改的官方验证器，成功以其结果为准。
- 顺序执行单 Agent。任务预算与整轮剩余 Token 预算取最小值。保留失败和未运行任务；
  记录最后一个响应的超量。调用用量未知时停止整轮，不将未知用量视作零消耗。
- 输入/输出 Token、缓存输入、步骤、工具调用、耗时、失败与原始 Trace 留档。
  按用户要求，公开评测汇总不计算或展示费用。

## 受信任准备路径的例外与迁移

当前固定编排代码的源文件下载、镜像下载、容器生命周期和参考测试使用
`ProcessRunner` 或下载 adapter，尚未全部纳入 Tool Registry 与 Permission System。
这是仓库“所有外部副作用必须经 Tool Runtime 和 Permission System”约束的局部例外，
仅适用于用户显式调用的评测准备路径；模型动作仍必须经过完整工具执行链。
原因是参考解与验证器不能作为模型可调用工具暴露，且准备阶段尚无 Agent Runtime。

替代方案是建立独立的管理员 Tool Registry，并注入路径、网络和容器权限服务。
迁移时应将固定下载主机、digest 拉取、受控复制和容器操作封装为管理员工具，
使用独立授权与结构化事件，同时保持模型 Registry 中只有既定的 `shell` 与 `git`。
迁移前 CLI 不提供任意宿主命令或任意下载 URL 参数，来源限定为固定上游与
用户显式指定的 TLS 镜像主机；这些编排日志不能冒充完整的 Tool Runtime Trace。

## 替代方案

宿主执行无法提供公开代码所需的隔离；复用旧 Docker runner 的可写挂载不能保证
验证环境独立；把 Mini-dsh 安装到任务镜像会改变预置依赖。因此采用宿主编排、
无挂载的持久 Agent 容器与单独验证容器。

## 后果与限制

这是 Token/时间受限的自选子集，不是官方 50 分钟协议成绩。
当前工具集合固定为 argv 与 Git；不能直接与本地五工具 baseline 归因比较。
Agent 容器可以修改其自身文件系统，操作系统边界是完整容器而非进程内路径校验。
容器引擎与 Linux VM 是受信任边界，不宣称抵抗内核或虚拟化逃逸。

验证器也默认断网。某些 Go 等任务仍需下载依赖；参考门禁失败时必须停下，
保留诊断，不扩大为通用 bridge 网络。后续可增加独立的、按目的地主机控制的依赖
准备服务，或者固定已预置依赖的新镜像，并重新运行门禁。CLI 不自动安装 Docker、
接受订阅协议、修改全局环境或开启容器权限。

当前入口分为 `prepare`、`check`、`run`；不支持中断后模型运行的自动恢复。
持久化的是每题结束时的完整清单报告；进程被强制终止时可能需要从 Trace 恢复 Token
事实。禁止覆写已有结果，以免自动重跑产生未计入的消耗。

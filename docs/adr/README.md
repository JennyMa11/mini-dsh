# Architecture Decision Records

重要架构决策在本目录按 `NNNN-short-title.md` 命名。每条 ADR 至少包含：背景、决策、替代方案、后果与状态。

| ADR | 决策 | 状态 |
| --- | --- | --- |
| [0001](0001-runtime-architecture.md) | 端口驱动的单进程 Runtime | Accepted |
| [0002](0002-security-model.md) | 默认拒绝的本地安全边界 | Accepted with limitations |
| [0003](0003-context-strategy-port.md) | Context 策略是端口，原始 Trace 是事实源 | Accepted |
| [0004](0004-bounded-tool-output.md) | 工具输出有界化，大结果落盘为 artifact | Accepted |
| [0005](0005-ablation-methodology.md) | 消融实验以单变量、可执行判据和脚本化模型为约束 | Accepted |
| [0006](0006-configuration-precedence.md) | 分层配置与来源可追溯 | Accepted |
| [0007](0007-interactive-terminal.md) | 交互式终端复用 Harness、跨轮历史与独立预算 | Accepted with limitations |
| [0008](0008-local-browser-gui.md) | 本地浏览器 GUI 复用 Harness 与权限系统 | Accepted with limitations |
| [0009](0009-explicit-runtime-extensions.md) | 显式组件扩展与命名运行配置 | Accepted |
| [0010](0010-sub-agent-collaboration.md) | 单进程 Sub Agent 协作、共享预算与只读子工作流 | Accepted |
| [0011](0011-gui-runtime-settings.md) | GUI 协作设置与内存凭证的边界 | Accepted |
| [0012](0012-session-facts-loop-factory-mcp.md) | Session 事实源、Loop 工厂插件与 MCP 工具接入 | Accepted with limitations |
| [0013](0013-local-first-small-model-harness.md) | 本地小模型执行路径与双维对照 | Accepted |
| [0014](0014-frozen-local-study.md) | 冻结任务集、断点恢复与配对统计 | Accepted |
| [0015](0015-public-container-benchmark.md) | 公开任务的容器执行与独立验证 | Accepted with limitations |
| [0016](0016-lightweight-public-repair.md) | 本机轻量公开修复评测 | Accepted |
| [0017](0017-evidence-aware-harness-metrics.md) | 指标区分观测、独立判据和未测状态 | Accepted |
| [0018](0018-controlled-harness-study.md) | 公开修复题上的功能验收与受控配对实验 | Accepted |
| [0019](0019-personal-terminal-workflow.md) | 个人终端工作入口、固定测试与真实验证状态 | Accepted with limitations |
| [0020](0020-terminal-input-editor.md) | 中文编辑、内存历史与多行粘贴的终端输入适配器 | Accepted with limitations |
| [0021](0021-layered-credentials.md) | 全局与工作区密钥覆盖、首次配置持久化与敏感文件隔离 | Accepted with limitations |
| [0022](0022-lightweight-dsh-runtime.md) | 流式、审批、稳定会话、受控钩子与配置插件组合 | Accepted |
| [0023](0023-bounded-extension-upgrade.md) | 有界扩展升级、兼容 Hook 与通用完成决策 | Proposed |

| [0024](0024-controlled-skill-context.md) | 按运行加载 Skills、Context 组合与拒绝链接替换的读取 | Accepted with limitations |

| [0025](0025-final-visible-verification.md) | 可见验证末尾约束与完成策略顺序保护 | Accepted with limitations |

| [0026](0026-private-project-memory.md) | 项目 Memory 私有存储、来源与运行外维护边界 | Proposed |

| [0027](0027-full-work-startup.md) | 编码工具、Skills、Verification 与 Memory 的显式完整开发入口 | Accepted with limitations |

| [0028](0028-work-default-shell.md) | work 默认注册 shell、常用命令白名单与逐次审批 | Accepted with limitations |

## 状态含义

- **Proposed**：规划中的决策；尚未完成实现与验收。
- **Accepted**：当前实现遵循该决策。
- **Accepted with limitations**：决策成立，但存在已知且已记录的边界（例如本地模式不是容器级隔离）。
- **Superseded**：被后续 ADR 取代，保留以供追溯。

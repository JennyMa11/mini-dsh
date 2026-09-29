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

## 状态含义

- **Accepted**：当前实现遵循该决策。
- **Accepted with limitations**：决策成立，但存在已知且已记录的边界（例如本地模式不是容器级隔离）。
- **Superseded**：被后续 ADR 取代，保留以供追溯。

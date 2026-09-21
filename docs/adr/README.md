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

## 状态含义

- **Accepted**：当前实现遵循该决策。
- **Accepted with limitations**：决策成立，但存在已知且已记录的边界（例如本地模式不是容器级隔离）。
- **Superseded**：被后续 ADR 取代，保留以供追溯。

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

| [0028](0028-work-default-shell.md) | work 默认注册全部可用工具、shell 白名单与逐次审批 | Accepted with limitations |

| [0029](0029-open-workspace-file.md) | 工作区文件打开工具与超时状态修正 | Accepted with limitations |

| [0030](0030-interactive-budget-adjustment.md) | 交互会话内查看和修改四类运行预算 | Accepted with limitations |

| [0031](0031-architecture-checks.md) | 显式分层检查与独立证据输入解码 | Accepted |

| [0032](0032-cli-command-modules.md) | CLI 参数、离线查看命令与 work 配置分离 | Accepted |

| [0033](0033-cli-model-configuration.md) | CLI 模型选择与配置翻译的显式依赖 | Accepted |

| [0034](0034-trainable-agent-experiments.md) | 独立 Trainable Agent 实验包 | Accepted |
| [0035](0035-lab-gspo-swanlab.md) | 迁移 Lab GSPO 与 SwanLab | Accepted |
| [0036](0036-minirepo-interaction-experiment.md) | 小仓库交互修复实验 | Accepted |
| [0037](0037-interaction-learnability.md) | 交互可学习性优先与单变量工具示例 | Accepted |
| [0038](0038-batch-read-learnability.md) | 可选批量读取与反馈后的修复机会 | Accepted（离线开发） |
| [0039](0039-batch-path-limit.md) | 固定输出上限下的批量读取路径数 | Accepted（离线开发） |
| [0040](0040-feedback-coverage-audit.md) | 公开反馈的离线缺陷覆盖审计 | Accepted（离线审计） |
| [0041](0041-interaction-learning-readiness.md) | 交互学习的新家族协议与完整预算准备 | Accepted（离线协议） |
| [0042](0042-learning-fixtures.md) | 新家族任务及反馈覆盖矩阵 | Accepted（离线夹具） |
| [0043](0043-learning-trajectory-bridge.md) | 原始轨迹与训练Batch的独立版本绑定 | Accepted（离线桥接） |
| [0044](0044-learning-orchestration.md) | 固定队列、完整资金准入与确认后恢复 | Accepted（离线调度） |
| [0045](0045-learning-provider-adapter.md) | 显式SDK适配、持久策略身份与反馈证据 | Accepted（离线契约） |
| [0046](0046-learning-execution-admission.md) | 固定执行配置、历史数值组件复核及初始LoRA门禁 | Accepted（离线实施） |
| [0047](0047-closed-period-budget-coverage.md) | 整账期覆盖审计与不丢消费的财务结转 | Accepted（条件结转） |
| [0048](0048-confirmed-stage-funding.md) | 固定完整队列与确认阶段的资金准入 | Accepted（离线实现） |
| [0049](0049-supplemental-closed-hour-coverage.md) | 已计入整段账单的补充覆盖与资金基线校正 | Accepted（条件应用） |
| [0050](0050-grpo-learning-curve.md) | 单 seed GRPO 学习曲线与新判据版本 | Accepted |
| [0051](0051-development-feedback-v3.md) | 开发反馈 v3，仅重新验收可学习性 | Accepted |
| [0052](0052-learning-curve-v4.md) | 通过 v3 准入后冻结单 seed 学习曲线 v4 | Accepted |
| [0053](0053-initialization-recovery.md) | 初始化失败的一次隔离恢复与结构化诊断 | Accepted |
| [0054](0054-equivalent-empty-optimizer-start.md) | 空优化器起点的可验证重建 | Accepted |
| [0055](0055-positive-control-and-policy-v2.md) | 正控 v1 与候选策略 v2 | Accepted（执行入口待实现） |

| [0056](0056-gspo-efficacy-study.md) | 按历史 GRPO 规模验证 Base 对 GSPO | Accepted |

| [0057](0057-gspo-terminal-backward-rollback.md) | GSPO终止反传任务的完整优化器回滚 | Accepted |

## 状态含义

- **Proposed**：规划中的决策；尚未完成实现与验收。
- **Accepted**：当前实现遵循该决策。
- **Accepted with limitations**：决策成立，但存在已知且已记录的边界（例如本地模式不是容器级隔离）。
- **Superseded**：被后续 ADR 取代，保留以供追溯。

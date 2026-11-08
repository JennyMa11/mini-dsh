# ADR 0034：独立 Trainable Agent 实验包

- 日期：2026-11-06
- 状态：Accepted（独立实验包已实现；效果与验收范围见结果报告）

## 背景与范围追加

用户明确要求规划 Coding Task→多轮Rollout→Trajectory→Reward→GRPO→Evaluation。
AGENTS.md §1规定Mini-dsh不训练/微调，extension-roadmap亦排除了模型训练。本ADR记录本次请求的范围追加及未来实施边界，不将现有核心改成训练框架，不重签既有M0–M5验收。

## 决策

在 `experiments/trainable_agent/` 建立独立依赖锁与训练控制器，通过既有Model port和composition运行原AgentLoop；PyTRIO远端实现LoRA数值计算，实验层负责数据、reward、mask、预算和验证。核心domain/application/runtime不依赖PyTRIO、Torch或实验模块。实验发起的工具副作用继续经受控工具/权限和现有基础设施，不向模型开放训练控制面。

首阶段只做Smoke；后续按[阶段门禁](../trainable-agent-rl/implementation-roadmap.md)推进。新能力属于独立研究扩展，默认关闭，不改变生产模型、工具和评测历史。

## 备选及代价

1. 把训练直接加入AgentLoop：耦合厂商、Token和optimizer，不采用。
2. 完全独立重写rollout agent：失去同Harness对照和既有安全能力，不采用。
3. 独立实验包复用Runtime：依赖隔离清晰，但需要Token旁路、事务账本及额外测试，采用。

## 迁移与退出

先冻结现有源码和基线；离线契约测试之后才接远端。若缺少端口能力，另提最小接口修改并跑回归。撤销实验仅需停用实验入口，生产Runtime无需迁移；保留原始证据与checkpoint索引。训练SDK依赖不得进入核心默认安装。

初始文档为Proposed。后续用户明确设定“完成50¥的核心版实验”目标，在累计50元上限内完成S0、主训练及配对评测；本ADR仅接受独立实验包边界，不追认正式RL收益、完整自动断点续跑或既有M0–M5全部验收。事实与限制见[结果报告](../results/trainable-agent-core-2026-11-06.md)。

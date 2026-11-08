# 学习收益与算法比较的准入复核

日期：2026-11-08。依据[原学习准入方案第 6、8 节](interaction-learning-readiness.md)
及[完整实测结果](learnability-grpo-results.md)。本次只解析已有训练 Token，并在 CPU
对 loss 公式做微分复核，没有加载模型、采样、训练、重跑判据或产生服务费用。

## 当前决策

**不进入算法效果比较，目标尚未完成。** 原方案明确：成功率差不为正，或提升仅来自
单一家族，不进入算法效果比较；即使单次正向，也须用独立训练 seed 和新留出家族
复核，才能称稳定学习收益有证据。当前 Base 7/16、RL 4/16，只有一个训练 seed，
未满足该门槛。权重确实更新、Readiness 通过、整轮完成或账单可用都不能替代它。

之前的 `algorithm-comparison-v1/protocol-02.json` 保留为前瞻候选，不是执行许可。
新增 `gate-review-01.json` 与 `protocol-03-hold.json` 记录当前不准入决定，不重写
原协议或参数，也没有生成可执行队列。这是审查结论，尚不存在新算法执行入口中的
程序门禁；不能宣称已经实现了自动拦截。

## 用真实轨迹核对两种 loss

源码依据：实验包 `trainer.py:normalized_batch`、`datum.py:build_batch`、
`contracts.py:training_arrays` 与 `gspo.py:make_loss`。逐组重读全部 32 条训练轨迹的
原始 completion Token/logprob，使用现有 GSPO 实现并对数学上的 native PPO 目标
做自动微分。这里刻意取 **p=q 的理想分析点**，不是调用当前模型重算 logprob；不
把分析梯度当作远端训练实际梯度或参数梯度，也不消除已知跨引擎偏差。

记第 i 条轨迹本次生成的 assistant Token 总数为 Lᵢ，G=4，组中心化奖励为 Aᵢ。
在 p=q、未触发 clipping 时，对任一该轨迹 action logprob：

- 本轮 native GRPO：`dL/dlogp = −Aᵢ / ΣⱼLⱼ`。
- 现有 GSPO：`dL/dlogp = −Aᵢ / (G × Lᵢ)`。

核对结果：8 组全部满足公式，历史上下文与 Observation 对应位置梯度为零。固定
记录中 5 组奖励全零，两个目标在这些组的策略梯度均为零。因此改变 loss 不能从
**这批固定全零奖励数据**产生任务成功信号。未来 GSPO 的新 on-policy 轨迹可能
不同，本分析不能预测新训练也恰好有五个零信号组。

3 个非零组内，GSPO 相对 native 的每 Token 梯度系数比介于 **0.523–3.804**。
原因包含轨迹长度重加权：GSPO 在轨迹内平均，native 对整组 assistant Token 平均。
这些比值不是参数更新大小或成功率预测，也不证明任一种权重更好。

本地证据：`benchmark-results/trainable_agent/algorithm-comparison-v1/` 中的
`analyze_loss_weights_01.py`、`loss-weight-analysis-01.json`，包含 32 条轨迹的长度、
奖励、梯度系数和输入 hash，可离线复核。无需 API Key。

## 后续比较必须先明确研究问题

| 研究问题 | 需要控制的变量 | 当前状态 |
| --- | --- | --- |
| 整套 native GRPO 与现有 GSPO 适配哪个更好？ | 相同数据协议、模型、初始权重、采样、Reward、Harness、更新组数；分别报告实际计算成本 | 可作为未来系统配方比较，但不能把差异全归因于 ratio |
| 仅 sequence ratio 相比 token ratio 是否有收益？ | 两臂均按相同 trajectory mean；匹配 clipping、Advantage、optimizer，并尽量匹配计算路径 | 当前 native 对照未匹配，不能直接回答 |
| 长度权重本身是否影响行为？ | 固定 token ratio，仅改变整组 token mean 与轨迹内 mean | 应作为独立问题；不能与换 ratio 同时改动后归因 |

原候选 clip=0.2 的 GSPO 是显式变体，不是其默认窄 clipping 配方。匹配 clip 只控制
了一个变量，没有控制长度权重。若未来研究仅 ratio，应先定义匹配对照并完成组件
验收，再冻结新的前瞻协议；不通过本轮 test 挑最优 clip 或最优 loss。

## 先解决可学习性的下一步

目前优先问题来自实际证据：零信号组消耗约 60.5% 训练 Token，三对退步分别涉及
迟迟不改、原样复测、修好后继续改坏。先在独立开发材料上制定反馈利用、最小修复
和终止决策的明确标签；不更改这轮冻结 Reward 或 test，也不把更多 patch、读取更少
或正常复测减少直接定义成任务成功。

后续如获准开展新学习实验，应先预声明新的开发/训练/留出划分及预算，先验证有
混合 Reward 和真实反馈恢复，再用固定算法验证收益。初次单 seed 小样本改善只
支持继续研究，不能跳过独立复核。当前不自动增加 G、训练步数或模型规模。

当前账本可用 2.765 元，精确实付仍待核验。即使后续账单释放余额，科学门槛仍是
独立条件；不因资金充足就执行被门槛拦住的算法对照。不缩减既定 test 分母来挤出
实验预算。下一轮付费实验仍需有完整、可审查且获授权的具体方案。

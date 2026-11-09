# 正控 v1：固定小任务可学习性

日期：2026-11-09。状态：协议冻结，队列生成器与契约 fixture 执行入口已实现，provider 适配器待接。

本协议只回答一个问题：**在固定少量训练题上，GRPO 管线能否产生明显的新采样成功率提升？**
它不是泛化实验、不是算法收益比较，也不能替代 [v4 学习曲线](learning-curve-v4-recovery.md)。

## 结果解释

| 结果 | 解释 | 下一步 |
| --- | --- | --- |
| 通过 | 管线能在固定小任务上过拟合 | 测同家族另外 9 道训练题，区分表面修复与可迁移修复 |
| 失败 | 瓶颈在优化、奖励或探索，不在泛化 | 先查奖励/优势、更新后策略漂移、难度分层 |
| 不确定 | 信号太稀疏 | 调 G 或题难度；不判 RL 无效 |

正控通过只说明“这套管线能拟合这几道题”，不宣称家族均衡、稳定收益或自然任务迁移。

## 前置修复

v4 的最终验证使用 `curve_pure_v1`，但运行中的 `run_tests` 仍使用 legacy 策略。全量 192 条轨迹中
有 28 条出现运行中 `candidate_policy_rejected`，共 561 次；17 道使用 `issubset` 的候选中有 16 道
在训练中被误拒，而规格明确允许 `set.issubset`。此外，最终 20 条策略拒绝中有 6 条是 `.rstrip`
造成的假阴性，其中 2 条能通过私有测试。

因此正控必须使用 `curve_pure_v2`（在 v1 基础上加入 `rstrip`、`lstrip`、`replace`），并让运行中
`run_tests` 与最终验证使用同一策略。v4 官方分数保持不变，另出离线重算报告。实现见
[minirepo_verification.py](../../experiments/trainable_agent/src/minidsh_rl/minirepo_verification.py)
与 [minirepo_tools.py](../../experiments/trainable_agent/src/minidsh_rl/minirepo_tools.py)。

离线重放 v4 的 192 条最终候选：v1 拒绝 20 条，v2 拒绝 8 条；减少的 12 条全部只因 `.rstrip`，
其中 6 条在跳过策略检查后能通过私有测试。作为诊断参考（不改写官方分数），v4 验证成功数
从 7/24→8/24→6/24→8/24 修正为 8/24→8/24→8/24→10/24，路由家族从 3/8→0/8 修正为 4/8→2/8。
路由退化仍然存在，但“归零”主要是策略漏判。

## 固定题集

每家族 1 题，共 3 题：

| 家族 | 题目 | v4 训练成功 |
| --- | --- | ---: |
| match_rank | `curve_v4_path_router_02` | 4/8 |
| dependency_batches | `curve_v4_dependency_batches_01` | 3/8 |
| table_join | `curve_v4_table_join_02` | 4/8 |

选择规则：每家族选 v4 训练成功率最接近 4/8 的题，平手取较小 `case_id`。这是**可行性选择**，
不是代表性抽样，不支持泛化结论。

基线可行性窗口：每题在 8 个 held-out seed 上成功 1–4 次。超出窗口按预声明回退列表替换，
最多替换 2 轮；仍不可行则在训练前停止并报告，不进入训练。

回退列表（按优先级；仅前 3 个进入基线队列，第 4 个为未采样后备）：

- match_rank：`02`、`01`、`03`、`00`
- dependency_batches：`01`、`03`、`02`、`00`
- table_join：`02`、`00`、`01`、`03`

## 冻结环境

Qwen3.5-4B、LoRA r16、seed17、G4、`tool_example`、`batch_read_eight`、12 步 / 12 工具调用 /
3072 生成 token / 36000 总 token、LR 1e-5、native PPO clip、assistant-token-mean、无 reference KL、
无组内标准差归一化、已验证空优化器起点。除题集、轮次、评估 seed 和 candidate policy v2 外，
其余与 v4 相同。

## 队列

- 训练：6 个 epoch × 3 题 = 18 组，每组 G4 = 72 条。混合奖励才更新，零信号组跳过。
- 训练 seed：`971000 + group * 100 + repeat`（group 0–17，repeat 0–3）。
- 固定洗牌 seed 29；每个 epoch 三题各出现一次。
- 评估检查点：0 / 6 / 12 / 18 组（即 0 / 2 / 4 / 6 个 epoch）。
- 评估：每题 8 个固定 seed，共 24 条/点；4 个检查点共 96 条。
- 评估 seed：`981000 + task_index * 100 + repeat`（task_index 0–2，repeat 0–7），与训练 seed
  不相交；同一 seed 跨检查点复用，形成配对比较。
- 检查点 0 同时作为基线可行性门和主比较基线，不重复采样；替换题只重测基线，不改变训练主题集。
- 所有失败保留在 24 条分母；不删题、不补采样、不按最佳检查点选终点。

## 判据

设 `b` 为检查点 0 的 24 条成功数，`f` 为检查点 18 的 24 条成功数。

- **通过**：`f >= 20/24`，`f - b >= 8`，且每题 `>= 5/8`。
- **失败**：`f <= 16/24`，真实 optimizer 更新 `>= 8`，且无安全、预算或连续零信号停止。
- **不确定**：其余情况（17–19，或更新少于 8，或提前停止）。

同时报告：逐题成功数、逐检查点成功数、实际更新数、零信号组数、裁剪比例、Token 用量和最终
adapter 参数差。完整策略熵不测，不用 sampled surprisal 代替。

## 费用与证据

运行器仍记录保守上界约 48.03 元（216 条：72 条基线 + 72 条训练 + 72 条评估）；本协议不做
预算优化或审批讨论。
执行时保留 `plan.json`、`source.tar.gz`、`rollouts/`、`candidate.json`、`verification/policy.json`、
训练审计和评估结果。隐藏判据不进入工作区，原始轨迹保留。

## 实现入口

`minidsh_rl.positive_control` 提供纯本地、无模型调用的队列生成：

- `make_baseline_plan()`：冻结 72 条基线队列（9 个候选 × 8 seed）。
- `select_tasks(...)`：按 1–4/8 窗口和冻结回退顺序选题；全部不可行则抛错。
- `make_plan(...)`：冻结 18 组训练 + 3 个检查点的 72 条评估。
- `load_baseline_plan` / `load_plan`：重算并拒绝被篡改的计划。

`minidsh_rl.positive_control_run` 提供注入式执行状态机：

- `prepare_positive_control(root)`：写 `baseline-plan.json`，不创建客户端或花费。
- `execute_positive_control(root, runtime)`：baseline → 选题 → 冻结 `plan.json` → 训练 → 评估 → `final-report.json`。
- runtime 由调用方注入；当前测试使用 `contract_fixture`，覆盖通过、失败和零信号停止三种路径。
- provider 适配器、初始 checkpoint 和财务租约不在本入口内；付费运行必须显式接入并授权。

执行产物：`baseline-results.json`、`plan.json`、`state.json`、`rollouts/`、`updates/`、`final-report.json`。

## 与 v4 的差异

| 项目 | v4 | 正控 v1 |
| --- | --- | --- |
| 训练题 | 12 题 × 2 轮 | 3 题 × 6 轮 |
| 训练组 | 24 | 18 |
| 评估题 | 12 validation × 2 seed | 3 train 题 × 8 held-out seed |
| 评估点 | 0/8/16/24 | 0/6/12/18 |
| 候选策略 | `curve_pure_v1`；运行中误用 legacy | `curve_pure_v2`，运行中与最终一致 |
| 终点选择 | 固定 0 对 24 | 固定 0 对 18 |

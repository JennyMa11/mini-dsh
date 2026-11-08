# H2 批量读取：离线准备记录

> 后续更新：[完整32条结果](batch-read-results.md)已完成，两组均3/16；处理组仅1家族
> 有验证的反馈修复，原门槛未过。新增估价2.094元，当前保守累计44.013元、可用5.987元，
> pending=0，训练更新0。下文0/32和41.919元为准备阶段历史时点。

日期：2026-11-07，分支 `experiment`。协议见[方案](batch-read-plan.md)，边界见
[ADR0038](../adr/0038-batch-read-learnability.md)。目标仍为先验证交互可学习性，再比较RL算法。

当前真实模型对照0/32、训练更新0；首次资金检查未准入的准备目录保留。
随后[四小时覆盖审计](pilot-hour-reconciliation.md)已释放6.102元保守预留，当前累计
41.919元、可用8.081元、pending=0；完整队列具备2.88元容量，尚未开始采样。
下列脚本均为工程证据，不能解读为真实模型已经学会反馈修复。

## 已实现及观测

- 实验工具提供可选read_files；默认四工具及original Prompt保留。
  Schema、整批路径校验、只读权限、容器符号链接拒绝、共享5000字符和显式截断有契约检查。
- 相同8个公开文件的确定性控制：single_read为12步，首次失败反馈在第10步；
  batch_read为7步，首次失败反馈在第4步。两条最终候选独立通过，修改中间候选独立失败；
  single_read终态budget_exhausted仍单列保留，不以最终成功掩盖预算状态。
- 本地ModelScope缓存精确渲染：控制组初始输入981–1009 Token，处理组1131–1159，
  新Schema固定增加150 Token。两组加1024输出均低于6144；控制组16个历史初始prefix
  逐Token一致。只检查初始上下文，后续路径与累计额度仍取决于模型动作。
- 前瞻性评分允许第k个候选在公开测试失败后再修改，但要求批量读取早于失败反馈，
  且部分候选经私有复核失败。迟到批量读取和原候选已通过均不能满足机制覆盖。
  合成评分夹具验证该规则，不混入科学样本，也不改变旧H1首次Patch口径。
- 准入测试覆盖缺少控制、控制包含模型调用、信息不一致、源码hash过期和资金不足；
  这些情况必须在模型/SwanLab连接之前拒绝。完整32条分母不因余额缩小。

所有改动局限独立实验包及文档，未修改核心AgentLoop、插件或安全边界。
本轮没有添加模型算法、训练依赖或新任务，旧原始Trace及结果保持冻结。

## 证据目录与复核

本地根目录 `benchmark-results/trainable_agent/batch-read-v1/`：

| 证据 | 范围与解释 |
| --- | --- |
| control-02/control-summary.json | 当前源码的脚本控制；new_model_calls=0，scientific_rollouts=0，synthetic_token_ids=true |
| task-gates-02/summary.json | 原8题56项参考/错误/常数/逐缺陷mutation/公开判据检查；不算模型成功率 |
| rendering-02.json | 16种初始渲染及16个历史prefix绑定；source/hash与本地tokenizer身份 |
| paired-preparation-01/ | 固定32行queue、protocol、源码归档及实际admission；不作为已运行模型结果 |
| quality-gates-01.json | 本轮实际静态/类型/回归命令、退出码、输入hash；缺失或失败不得写为通过 |
| status-01.json | 准备及证据审计汇总；模型成功率未测，完整目标未完成 |

control-01/task-gates-01/rendering-01保留为较早源码证据，不冒充当前版本。
源文件改变后应重新生成绑定证据；新输出路径必须尚不存在，不覆写历史。

```bash
# 无模型调用；new替换为尚不存在的目录/文件名
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_batch_probe \
  --output benchmark-results/trainable_agent/batch-read-v1/control-new
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_gates \
  --output benchmark-results/trainable_agent/batch-read-v1/task-gates-new
uv run --project experiments/trainable_agent --extra lab python \
  experiments/trainable_agent/scripts/preflight_batch_rendering.py \
  --baseline benchmark-results/trainable_agent/interaction-learnability-v1/paired-01 \
  --output benchmark-results/trainable_agent/batch-read-v1/rendering-new.json
```

## 当前资源与未完成项

首次准备时账本保守占用48.021元、可用1.979元，完整队列差0.901元而被拒绝；
之后按完整账期覆盖审计校准到41.919元，可用8.081元，无pending，累计上限50元。
完整32条需预留2.88元；本轮离线工作没有新增provider消耗。
实际项目最终账单未知。只在既有收费覆盖证据充分时继续对账，不释放管理操作、历史
及不确定预留来凑队列；不清零消费，不使用重置卡，不自动提额。

尚缺真实模型是否使用新工具、反馈后的修改与独立成功、完整配对效果及不确定性证据。
因此不能开始新RL Pilot或GRPO/GSPO比较。费用覆盖现已复核，下一步核对当前报价、
全部离线输入与源码，再按冻结H2协议评估；若机制未过，保留全部负面结果并另定单变量假设。
完整Memory/Skills、恢复、Secret/网络攻击矩阵本阶段未重验，工程回归不补认这些能力。

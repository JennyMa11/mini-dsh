# GRPO 学习曲线 v4

> 后续已完成：[v4 恢复与完整学习曲线](learning-curve-v4-recovery.md)，192/192 条，17 次更新，
> 固定终点 8/24 对基线 7/24，探索门槛通过但稳定收益未证实。下文保留 run-01 原始停点。

日期：2026-11-09。范围依据 [ADR0052](../adr/0052-learning-curve-v4.md)。
当前阶段固定单训练 seed，回答增加训练后是否出现可测学习；不开展算法对照或多 seed。

## 冻结协议

复用已通过的 v3 开发准入：8/24 成功，4/6 混合 G4 覆盖两个算法组，3 条反馈后修复
覆盖两个算法组。复核历史源码归档、全部轨迹及反馈 sidecar，不增加开发调用。
新版本仅含 12 train 和 12 validation，排除全部六道已打开开发题。业务源码、规格、
缺陷与私有判据沿用 v2，公开测试沿用 v3 逐合同反馈，需完整 336 项预设探针通过。

固定模型 Qwen3.5-4B、LoRA r16、原初始 optimizer、seed17、G4、原提示、工具、奖励、
采样与已校准数值核。两次固定 shuffle 覆盖 12 道 train，24 个尝试槽共 96 条轨迹。
同家族 12 道 validation 每题两个固定 seed，在尝试槽 0/8/16/24 各运行 24 条，共 96 条。
零信号组占用尝试槽并跳过 optimizer；按验证分数选择终点被禁止。

主比较为固定 0 对 24 的配对成功差，学习门槛：差值 >0 且至少两个算法家族改善。
使用固定 seed 的家族聚类 bootstrap 10000 次给出探索区间，不把单 seed 作为稳定性证据。
连续十个零信号组、fresh-policy 裁剪比例超过 5%、安全或未知事务触发停止。
完整策略熵未测，仅报告 sampled surprisal，不以代理指标替代熵。

恢复原始 optimizer 后保存 sampler，下载新 adapter 与已准入原始 adapter 逐 tensor 核对
差异为零。训练后若有实际更新并完成队列，再核对真实权重变化。
所有采样原始 token/logprob、评分证据和财务关联留存；未运行项保留在声明分母。

继续唯一累计 150 元/新增 100 元 epoch，开跑基线占用 52.104 元；本阶段额外上限 60 元，
完整队列预留上界 56.706 元。不建立新财务档位、不自动结算旧费用。

## 可复现入口

实验包入口 `python -m minidsh_rl.learning_curve_run`：audit → prepare → inspect → run。
prepare 绑定独立历史准入、24 题 manifest、源码归档、配置、初始 optimizer/sampler 与账本基线。
run 接入真实服务前重新校验冻结输入及完整剩余预算；未知事务不重放。
离线报告入口为 `minidsh_rl.curve_analysis` 和 `minidsh_rl.curve_training_audit`。
本地原始产物目录为 `benchmark-results/trainable_agent/learning-curve-v4/`，不提交轨迹或模型缓存。

## 执行结果

336/336 项数据检查通过：24 个参考解均通过公开/私有检查；144 个预声明错误候选
均被两套检查检出。168 个候选中有 49 个唯一程序；这是有限预设探针的覆盖，
不是独立自然任务或完整语义证明。历史 v3 准入复核通过，未重新采样开发题。

完整队列冻结后，2026-11-09 01:18:38–01:19:25 UTC 实际执行约 47.12 秒，退出码 1。
恢复客户端注册后，在保存初始 optimizer checkpoint 的惰性 actor 初始化提交处抛出
`ActorEventSubmitError`。安全 traceback 已留存；服务端具体根因未确认。
后续只读匹配到一个 owned 恢复任务：Qwen3.5-4B/r16/seed17、restore_optimizer=true，
状态 ENDED、checkpoint 数量 0。该查询不创建客户端、checkpoint、采样或优化请求。

新采样 0，训练轨迹 0/96，验证轨迹 0/96，optimizer 更新 0；四检查点均未测。
初始 adapter 等价性与最终权重变化均未测。**学习效果未测**，成功率、配对差、
有效更新率、裁剪比例及完整策略熵均保持 null，不能将未运行项写为 0% 成功率，
也不能据这次服务失败判定 GRPO 无效。

控制器原始状态保留为 running/pending=initialize；派生执行结果明确标为
initialization_stopped_unknown_checkpoint，不覆盖原状态冒充正常结束。
没有 initial.json/确认保存回执，因此不满足原有安全恢复条件；未自动重试或重放。

本阶段保守占用新增 0.160 元：metadata 0.050、恢复客户端 0.010 为已知调用估计，
初始 checkpoint 保存 0.100 为未知请求预留。累计占用 52.264 元，pending 一项；
这些是本地保守记账，未认作服务已结算账单。唯一 150/100 元 epoch、历史分数与
冻结计划未改，没有新建账本释放预留。

[数值停点报告](../results/grpo-learning-curve-v4-2026-11-09.json)与
[判据覆盖报告](../results/grpo-learning-curve-v4-coverage-2026-11-09.json)
包含版本、完整分母与证据 hash。两次离线派生报告完全一致，不计作再次实测。
原始轨迹目录没有新增 rollout；独立训练审计确认零组、零更新、零优化提交。
226 份冻结源码/锁文件归档成员逐 hash 复核一致。

## 工程验证与下一入口

根项目 Ruff format/check、175 文件 strict mypy、1072 项 pytest 通过；实验包
190 文件 strict mypy、318 项 pytest 通过。新增验收覆盖排除 dev、固定 192 行、
复用准入、未绑定准入拒绝、完整队列调度、预算上限、真实脚本桥接，以及预算不足
不创建 provider 和中断消耗保留。脚本/假 SDK 证据只支持工程契约。

运行前后真实容器安全探针通过：无宿主挂载、网络隔离、凭据环境缺失、超时/取消
子容器清理及宿主 canary 不变。运行后精确凭据检测覆盖 36576 份受检 artifact，
未发现配置中的凭据文本；不推定其他 Secret 的全域零泄漏。没有遗留实验容器。

下一步先审计未知保存请求的远端事件/账单并定位初始化失败；当前只读列表不提供
足以结算该请求或认定权重恢复成功的证据。保留 pending，不在原 run 中重放未知
操作；恢复须有确认回执并满足原协议，否则应独立记录修复/重冻结方案。
当前不扩大样本、切换算法或改变数据难度。

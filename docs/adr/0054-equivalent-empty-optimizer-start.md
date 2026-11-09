# ADR 0054：空优化器起点的可验证重建

- 日期：2026-11-09
- 状态：Accepted（用户要求停止额外用量核对、继续实验）

两次 checkpoint 恢复均在 Actor 初始化提交返回 HTTP404/not_found，最新明确
retryable=false；尚无采样、梯度、优化提交或新 checkpoint。原始 Train checkpoint
已只读下载成功，安全 torch.load(weights_only=True) 观察 optimizer.state 为空，
不存在已训练动量。不能据 HTTP404 推定模型恢复方法普遍无效或源文件丢失。

下一执行使用相同模型/r16/seed17 重新注册 fresh LoRA。采样前必须下载新 sampler
和 Train checkpoint，要求所有 adapter 张量与原初始参数差为0，优化器 state 均为空，
param_groups 的所有非 params 配置相同，参数数目/唯一性相同；任一不符停止。
这仅适用于未更新的空优化器起点，不推广到训练后的恢复，不将新建称为严格 checkpoint resume。

同一 v4 192 行科学队列、奖励、验证 seed、原始起点、数值核和阈值保持不变。
保留两个初始化失败目录及未知预留，不再开展额外用量核对。执行隔离两条已结束、
无采样无优化的初始化记录；任何当前任务的未知请求仍停止。继续原阶段预算基线，
不重置限额。新的重建路径仅一次，无递归重试；失败则记录其错误和未运行分母。

## 结果

真实新建初始 checkpoint 保存成功，全部 adapter 参数差为0，空优化器配置一致。
run-03 完成固定192行，17次实际更新，最终 adapter 参数差 L2≈0.216397813。
两个先前恢复失败保留；最终成功8/24对初始7/24，探索门槛通过但未证明稳定收益。
完整结论与证据见 [学习曲线结果](../trainable-agent-rl/learning-curve-v4-recovery.md)。

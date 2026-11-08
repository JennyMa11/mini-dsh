# ADR 0043：以独立绑定连接小仓库轨迹与训练Batch

- 日期：2026-11-07
- 状态：Accepted（离线桥接；不准入付费队列）

## 决策

在新数据有限覆盖通过后，只在实验包新增typed轨迹桥接与原始采样边界记录。
复用原minirepo_rollout、工具、独立验证和build_batch；不修改Loop、旧result/Trace、
数据判据、权限、Reward或账本。桥接通过单独sidecar绑定LearningCase的新split、
dataset/criterion、policy snapshot和tokenizer身份，不重写旧development标签。

在SamplingBackend外记录每次sample的进入/返回及原始Turn摘要。存在未返回采样、
未持久化原始Token、断裂Trace或版本/候选不一致时拒绝训练；本地额度拒绝若没有
进入该边界，保留原Trace未知字段并独立说明，不生成不存在的Token。
控制用脚本必须显式标记scripted_control，禁止进入provider训练Batch。

组装G4时要求同case、同snapshot、同tokenizer、同Harness及四个不同run/seed，且
仅train split可进入；失败、预算终态及零Token失败保留在组内奖励分母。
优势仍为Reward减组均值；全组没有真实Token时返回无Batch的零信号组，不伪造动作。

## 验收及边界

先写边界契约测试，覆盖原始Token、mask、无效动作、独立verdict、未知采样、版本、
重复身份、跨组混入和脚本拒绝。再用固定脚本在真实Docker验证公开失败反馈后的修复
及独立判据绑定；这是Component/Trajectory证据，不是模型成功率或学习收益。

policy/snapshot身份来自可信实验控制器；hash和类型不能认证任意外来文件确由服务
采样。后续真实控制器仍须绑定SDK sampler/快照、完整费用准入及durable update journal。
本阶段没有ServiceClient创建、训练调度或恢复执行，不据桥接通过补认这些未测能力。
当前仍累计50元，完整阶段资金不足时不启动模型readiness、SFT、RL或算法比较。

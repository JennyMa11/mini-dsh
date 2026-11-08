# ADR 0046：冻结真实执行配置并复用未变化的数值组件证据

- 日期：2026-11-07
- 状态：Accepted（离线实施；真实执行未准入）

## 决策

提供prepare/inspect/run入口，默认准备及检查均不访问服务。只有显式run且完整资金、
固定配置、源码/SDK、数据和数值组件证据全部通过，才允许只读实时报价检查及创建SDK。
配置摘要进入plan，tokenizer/报价/历史数值输入各自固定hash，不接受任意passed=true。
活动账本与50元上限不变；本阶段不调用真实run，不运行模型或训练。

复核发现contracts.py、datum.py、trainer.py、pytrio_backend.py与原数值验收版本字节
一致。重新计算历史Mask/归一化/Adam恢复的实际权重差，校验当时归档源码与PyTRIO
0.2.9锁定版本后，可作为historical_component_reuse使用，不包装成新SDK数值实测。
若关键代码、SDK版本、renderer或tokenizer改变则复用失效，必须另定新数值探针预算。
每个真实新group仍使用既有fresh-policy 5% guard，之后才允许optimizer。

新执行入口额外要求下载本次initial Sampler，验证LoRA配置、tensor名称/形状及零B矩阵；
失败不得开始readiness。此项作为0.50元准备额度的一部分，保守预留0.02元组合管理额度，
不是已证实收费。Checkpoint显式记录准备占用，恢复不重复扣除，剩余分配不双计。
完整80条总分配仍20.302元，当前7.333元可用不足，不先执行一部分。

## 替代与边界

重新运行所有数值探针会产生多次clone、训练与导出，且不改变当前未变的核心组件，
因此优先复用可审计证据。该选择不证明服务端永不漂移，不证明新SDK适配已真实恢复，
也不降低H1–H3或新任务readiness门槛。真实服务漂移、未知RPC及任何证据缺口均停止。
所有实现位于独立实验包；核心Loop、插件、旧结果、旧账本及判据不变。

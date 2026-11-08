# ADR 0045：新任务的显式SDK适配与反馈证据

- 日期：2026-11-07
- 状态：Accepted（仅离线开发与契约验收）

## 决策及范围

在ADR0044控制接口上提供具体PyTRIO RuntimePort实现，显式注入service、已持锁的
CostStore、ProcessRunner及tokenizer pin，不在import/构造时创建服务、采样或训练。
复用PricedBackend、TrainerV2、learning_rollout和独立验证器，不重新实现GRPO内核。
新增公开失败反馈后的候选复评，保持旧H1–H3及私有判据不变。

每次有效更新保存Train与Sampler两类checkpoint，再发布统一回执；恢复使用
create_training_client_from_state_with_optimizer，Base仍绑定initial Sampler。
策略摘要绑定保存回执元数据，不冒充远端tensor文件hash。模型/tokenizer改变则停止。
未知操作仍由财务pending及外层状态阻断，不自动重试或更换policy。

本阶段只用假SDK客户端和固定脚本容器检查，不连接真实ServiceClient、不执行数值
Smoke/readiness/训练、不上传新SwanLab数据。活动上限50元及历史账本保持不变。
新的真实数值与资金门禁仍须在真实入口开放前完成，适配代码可调用不等于已准入。

## 依据、替代与后果

依据固定本地PyTRIO 0.2.9源码，以及官方[保存](https://docs.pytrio.com/docs/guide/save)
与[恢复](https://docs.pytrio.com/docs/guide/resume)接口。临时匿名sampler会过期，不用于
持久恢复；只恢复权重会丢失optimizer，不采用。全部功能在独立实验包，核心Loop不变。
采样客户端创建与对应Sampler保存合并在原0.10元管理额度内；恢复客户端及已保存
Base/current sampler重连合并在原0.01元额度内。它们是保守规划额度，未证明真实收费。
任一组合调用失败均保留pending，不能据已完成部分自动重放组合。

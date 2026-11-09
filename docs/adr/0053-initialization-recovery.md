# ADR 0053：初始化失败的一次隔离恢复与结构化诊断

- 日期：2026-11-09
- 状态：Accepted（用户在初始化停点后明确要求继续）

## 原因与边界

v4 run-01 的保存接口在 `_ensure_initialized -> _submit_init` 的 HTTP 提交处失败，
未进入 checkpoint 保存事件。旧诊断只保存异常类名，丢失状态码、错误码和请求标识。
两次 owned 只读核查均见恢复任务 ENDED、0 checkpoint，控制器仍 pending=initialize，
初始/当前 policy、轨迹、更新与 snapshot 均为空。控制台今天暂无用量日志；这不是零费用证明。

不重复旧请求、不释放/结算其 0.100 元未知预留，也不更改旧源码归档与失败结果。
替代方案是等待服务端逐请求回执（当前接口没有可查 event ID），或绕开恢复随机重建
初始权重（破坏起点一致性，拒绝）。选择仅一次、独立目录和新 owned 任务的原始 optimizer
恢复；先以确定性证据隔离已结束的初始化任务，再在同一账本计入全部旧预留。
这是 ADR0052 对“全部 pending 拒绝”的一个显式窄例外，不推广到采样、梯度或优化未知。

## 恢复门禁

冻结旧 plan/state/stop/budget 快照和最新 owned 只读证据，严格要求唯一任务、ENDED、
restore_optimizer=true、同原始 source、0 checkpoint、无本地 rollout/update/policy、
停止栈为初始化事件提交。仅豁免该唯一 operation 的阻塞判断；金额和 pending 状态仍保留。
任何新 pending、未知 optimizer、证据变化、存活任务或已有 checkpoint 都拒绝；
新任务创建前再次只读核验远端所有权、终态及零 checkpoint。
原失败目录只能认领一个恢复目录；恢复再失败不递归认领或自动重试。

恢复目录沿用相同 v4 数据、192 行队列、初始 optimizer、v3 准入、判据、采样、
reward 与数值协议。60 元上限继续锚定原 run-01 的 52.104 元基线，失败的 0.160 元
也在本阶段额度内，不迁移 epoch。数据检查输入和输出逐 hash 复用；只改执行诊断，
不能称为重新实测 336 项检查。真实初始 adapter 等价性仍须通过才采样。

SDK 每次 RPC 异常额外保存限长错误码、HTTP 状态、可重试标志和标识 hash，
不保存原异常文本、URL、headers、details 或凭据。恢复客户端注册后立即持久化身份，
保证惰性初始化失败也可定位远端任务。数值核、mask 和梯度算法不改。

## 验收与迁移

先测试未知保存保留预留、额外 pending 拒绝、证据篡改拒绝、默认入口继续拒绝、
一次认领及原基线不重置，再运行。旧 run-01 永久保持原始 pending；恢复目录独立报告
有效 pending 与隔离旧预留。学习与财务未决分开报告，不将例外当作已结算或预算满足。

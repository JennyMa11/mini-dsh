# ADR 0050：单 seed GRPO 学习曲线与新判据版本

- 日期：2026-11-08
- 状态：Accepted（用户确认计划并要求实现；收益待验收）

## 决策

沿用 ADR0034 的独立实验包；新建 curve_dataset_v1，使用 path_router、
dependency_batches、table_join 三个已见算法组的不同跨模块缺陷组合。
每组4 train、2 dev、4 validation，共30题，不宣称新家族泛化。
训练G4共24槽，在0/8/16/24槽验证；所有科学队列共216条。

curve_pure_v1显式允许issubset，旧判据默认行为不变。新旧版本保留独立标签；
旧Prompt、Reward、结果及Trace不重写。拓宽实验LearningCase的split枚举，
支持dev/validation；原train/test数据和值不变。

新增实验预算上限100元、累计名义上限150元须通过显式迁移契约实现，保留全部
历史消费、预留、operation和父hash，不直接放宽旧CalibratedBudget硬编码。
先完成420次容器判据检查，再实现/验收预算与曲线调度，通过完整准入才访问provider。
任何离线判据门禁失败阻止模型调用，记录失败类别与未运行分母，不临时换题。

## 备选及迁移

直接修改全局白名单会改变旧评分语义，拒绝；在独立进程里monkeypatch也缺少显式
绑定，拒绝。采用候选策略显式参数，默认legacy。停用新入口即可退出，核心Loop
与旧实验无需迁移。后续三seed/算法比较另设阶段，不由本ADR自动放行。

## v2实施与停点（2026-11-08）

用户明确继续授权后，新curve_dataset_v2仅修订私有尾斜杠输入并保留v1；420检查
通过。显式预算迁移、固定曲线调度、原始Token/财务旁路与确认恢复契约已实现。
真实dev24条成功7条，混合组2/6且仅1算法组、反馈修复1算法组，原准入失败，
故没有实际训练或checkpoint学习曲线。具体源码/证据及收尾预算单子epoch保护见
[结果](../trainable-agent-rl/learning-curve-v2.md)。历史分数不因v2通过而改写。

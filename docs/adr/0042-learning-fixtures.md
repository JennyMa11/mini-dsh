# ADR 0042：离线构建独立家族任务与反馈覆盖矩阵

- 日期：2026-11-07
- 状态：Accepted（离线夹具阶段；不准入模型或训练）

## 范围

按ADR0041及交互学习准入草案，将8个候选家族/16题实现为独立LearningCase，
保持train4家族/test4家族隔离。每题两个跨模块缺陷、两个额外语义mutation，
先写契约测试，再构建公开反馈及独立输入检查，用固定Docker顺序执行覆盖审计。
所有参考、mutation和私有判据仅留在实验控制面，旧开发任务、判据及模型结果不变。

复用MiniTask、现有候选纯函数策略、隔离验证器和覆盖统计；不改Loop、工具或权限。
新数据使用独立case/feedback/criterion版本，验证器原有sidecar字段保留，外层新
审计sidecar绑定新版本与实际checker hash，不把旧minirepo_dev_v1标签当新数据版本。

当前只交付数据及其有限覆盖证据；训练桥接在数据门禁后进入后续单独阶段。
不启动模型readiness、SFT、RL或算法比较，不更新已冻结财务/科学队列。
公开反馈检出预设mutation不等于穷尽语义覆盖，也不能证明模型可以利用反馈。

## 新旧分离及验收

每次执行前冻结完整probe manifest和源码，逐个记录public/private独立结果、拒绝、
超时、分母及hash；所有reference通过，已声明错误候选必须功能性失败。
策略拒绝、语法错误或超时不得算作反馈捕获。运行失败保留原目录，修复规格或夹具
须新审计目录；数据尚无模型结果，此时可修复判据本身的错误并记录原因。

规范化AST查重排除完整reference直接重复；共享contracts/validation不冒充独立业务。
另逐家族审阅主算法与历史数据关系，记录局限，不宣称预训练无污染或自动无泄漏。
只有离线数据门禁完成后再实现typed训练桥接；完整资金不足时继续禁止付费入口。

## 离线审查后的分组修订

224次有限检查通过，但router和media都属于匹配排序结构。首次模型调用前把二者
合并到同一family_group并都置于train，record_diff改到test；新dataset版本为v2。
这使train为3个算法组、test为4个，不能继续按8个独立组表述。候选、公开文件和
checker未变，旧容器结果逐hash重新绑定，不重新实测或覆盖旧审计。完整事实及局限
见[夹具结果](../trainable-agent-rl/learning-fixtures-results.md)。

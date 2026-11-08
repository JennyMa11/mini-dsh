# 确认阶段资金门禁：离线实现与验收

日期：2026-11-07；依据[ADR0048](../adr/0048-confirmed-stage-funding.md)。
本阶段实现并离线验收confirmed_stage_v1。未启动真实采样/训练、实时报价查询、初始
权重导出或账单结算；没有新的学习收益结果。原[可行性评估](staged-funding-feasibility.md)
保留为历史记录。目标仍是先验证交互可学习性，再比较算法，尚未完成。

## 已实现行为与边界

`full_remaining_v1`仍是默认，要求全队列剩余资金。显式选择新模式后，分别为完整
16条Readiness、下一完整训练组（4条rollout及其更新/保存）、完整32条test检查资金。
两种模式共享同一80条队列、8个训练组、case/seed/顺序/判据和20.302元完整分配。
核心Agent Loop、工具权限、插件和GRPO参数均未改。

资金不足只在已确认的阶段边界进入`awaiting_settlement`，保留所有未运行行。
没有新的占用下降时，重复显式resume直接保持等待，state文件不变，不进入preflight、
factory或SDK恢复，也不消耗恢复次数。存在pending或未知optimizer结果仍拒绝恢复；
已知累计超50元是预算违规，不能转成正常资金等待。

占用下降**不是账单覆盖证明**。本控制器不自动reconcile、释放旧估价、刷新网页或
认定退款；新增费用结算仍需请求归属、闭合账期、bill不重叠及独立审计。无法完成
结算时可以持续等待，整轮可能无法完成，不把未运行项从分母删除。

除阶段容量外，仍检查原实验累计新增占用加全部剩余动作不超过20.302元，及全局
50元上限。0.50元是共享额外额度；初始校验0.02和每次恢复0.01只扣一次。新模式
恢复上限12，旧模式仍2；SDK适配器必须显式接收相应上限。恢复次数或共享额度用尽，
在preflight/factory之前拒绝。训练引擎fresh-policy 5% clip门禁和初始零LoRA门禁保留。

## 模块与输入输出

| 文件 | 变化及证据意义 |
| --- | --- |
| `learning_schedule.py` | FundingPolicy进入plan；固定完整分配和逐模式恢复上限；prepare保存新源码 |
| `learning_execution.py` | next_stage_allocation计算当前完整阶段，另保留remaining_allocation；等待与未知请求分别处理 |
| `learning_prepare.py` | inspect分别报告整轮容量、下一阶段容量及所需金额；execution_admitted始终false |
| `learning_run.py` | prepare可显式选择模式；plan与execution-config双重绑定，不能只编辑plan放行 |
| `learning_sdk.py` | 真实factory按冻结模式注入2/12次恢复上限，默认不扩大 |

新冻结目录为`benchmark-results/trainable_agent/staged-funding-v1/prepared-01/`。
与旧prepared-01逐行比较，除实验ID前缀外所有科学字段一致；execution-config仅新增
资金策略，其余字段与旧文件一致。旧plan、源码archive和结果原样保留。由于当前
源码已经升级，旧plan应按旧归档复核；不要用新源码编辑其hash强行运行。

## 当前资金与运行许可

| 检查项 | 本次离线inspect |
| --- | --- |
| 活动保守占用 / 可用 | ¥42.667 / ¥7.333，pending为0 |
| 整轮剩余需求 / financial_capacity | ¥20.302 / false |
| 当前Readiness需求 / next_stage_financial_capacity | ¥2.15 / true |
| execution_admitted | false |
| 已运行的新研究行 | 0/80 |

活动账本SHA仍为`7e9712a184726e9f0013a3b28c5ebc5faee19bc4384f181366292dee8ce2dc7c`。
没有使用重置卡、提额或因旧Core轨迹数量吻合释放预留。2.15元包含初始化0.21、
16×0.09和共享额外额度0.50，并非精确报价；单训练组上界2.409、完整test上界3.38。
阶段分别可负担不意味着整个顺序现在可负担，后续依赖真实费用及结算。

显式run会先做当前阶段准入，再实时preflight并重验资金/配置，才创建SDK会话。
Readiness满足原门槛且资金足够时，控制器会继续后续训练阶段，直到完成、判据拒绝、
预算违规、未知请求或资金等待；它不是一个“只运行16条”的命令。用户此前要求
不要跑实验，这项指令仍有效；本阶段没有调用run，资金容量不代替实际运行许可。

## 验收与结论范围

完整回归：主项目1072项通过（52.09秒）、实验包273项通过（24.55秒）。Ruff检查
通过，526文件格式检查通过；strict mypy分别175/158文件通过。没有在运行中的
pytest或源码冻结检查期间修改文件；没有为文档补记重跑完整测试。

| 契约场景 | 实际检查 |
| --- | --- |
| 模式与科学队列 | 两种模式任务、seed、顺序、分母及完整分配相同；单改plan模式被config拒绝 |
| 阶段首个动作前缺资金/pending | preflight/factory均0调用；已知累计超限不进入等待 |
| 完整Readiness后缺下一组资金 | 完成16条后等待，训练0次，余64行保留；重复调用state字节不变 |
| 多次已确认费用下调 | 假账单场景跨9次恢复完成80行；一次初始化、8次optimizer，无重复行或更新 |
| SDK恢复上限 | 默认第3次恢复拒绝；显式12次策略的假SDK恢复成功，保存Train/Sampler身份一致 |
| 故障与零信号 | 两模式均检验确认回执恢复不双更新、初始准备费不双扣、零信号跳过、Readiness拒绝 |
| preflight后资金变化 | 再次准入失败，factory仍0调用；未知optimizer不重放；恢复耗尽在factory前拒绝 |

以上是contract_fixture或假SDK控制逻辑证据。假账单中的费用下调不是提供商实收
折扣；完成80条假轨迹不表示模型可学习，也不代表远端恢复、初始LoRA或闭合账期
结算已重新实测。旧数值组件继续按冻结证据复用，本阶段没有新增真实数值探针。

本地`staged-funding-v1/review-01.json`重新检查203个冻结源码成员及archive、原始
账本hash、计划/配置差异、JUnit数量和关键场景名。审计脚本不导入SDK或访问网络：

```bash
uv run --project experiments/trainable_agent --extra lab python \
  benchmark-results/trainable_agent/staged-funding-v1/audit_evidence.py \
  --output benchmark-results/trainable_agent/staged-funding-v1/review-recheck.json

uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_run \
  inspect --directory benchmark-results/trainable_agent/staged-funding-v1/prepared-01
```

证据与归档位于本地忽略目录，不提交账户/模型原始数据；新文件名不能覆盖旧结果。
下一步真实执行仍须取得运行许可；随后先观察Readiness是否产生混合奖励和独立反馈
恢复证据。未通过则停止训练，不依据test挑任务或改阈值。算法比较继续放在新任务
学习证据之后，不因本次资源门禁验收通过提前开展。

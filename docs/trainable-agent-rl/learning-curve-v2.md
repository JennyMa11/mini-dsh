# 单seed学习曲线v2：尾斜杠判据修订与执行记录

2026-11-08：用户明确要求继续补私有判据、冻结v2、重跑420检查，通过后落实
新增100元/累计150元预算迁移和曲线调度，再开发24条；通过才训练96/验证96。
沿用ADR0050与原学习曲线协议。v1结果不改写，继续保留门槛失败。

## 执行前冻结

curve_dataset_v2使用curve_v2新case ID、curve_private_v2判据；公开文件、训练/
开发/验证划分、参考解和全部缺陷组合与v1逐字节相同。仅为path_router追加私有
单路由/manual、/manual/与请求自身/子路径/尾斜杠组合，检查/manualx拒绝、其他
method拒绝、根路径及输入不变性。未复制公开/trail输入，也不向Agent提供私有输入。

420项清单在任何检查前冻结；完整重跑，不复用v1通过项。暂不改变提示、工具、
奖励与训练剂量。预声明开发准入仍为3/6混合G4覆盖2算法组、成功6–18/24、
至少2算法组真实公开失败后修复且独立通过。未过门槛停止，无补采样。

数据检查执行源码archive、后续调度源码archive分开绑定；即使数据通过，也要验证
后续代码使用完全相同的case/checker/hash，不能把数据验收替代新调度验收。

## 离线验收与真实队列配置

420/420复验完成：reference30/30在两套判据通过；180个错误实例在公开/私有
均被功能测试检出，无静态拒绝或超时。实际执行源码保存在audit-01/source.tar.gz。
新调度与预算代码在此后实现，重新逐项绑定数据检查证据，不伪称与审计源码相同。

curve_run提供prepare/migrate/inspect/run；prepare与inspect不创建provider会话，
run仅在完整216条队列资源及新迁移财务账本准入后执行。旧50元账本冻结结转，
父hash/全部operation ID/未决费用不可释放；新阶段硬上限100，累计150。
迁移以旧账本实际committed核对baseline，不允许伪造较低历史占用；未知父请求拒绝迁移。

固定G4、train seed17、12题两轮shuffle=24槽，0/8/16/24均验证12题×2重复。
dev seed=926000+题序×1000+repeat×20；train=17+slot×1000+repeat×20；
validation=936000+题序×1000+repeat×20。各检查点使用相同题/repeat seed，题序轮转，
不声称可以把初始和未来权重在同一时间交替执行。检查点按尝试槽计，不补零信号组。

通过原始真实Token/logprob桥接，明确仅curve_dataset_v2使用tool_example与
curve_pure_v1，旧数据入口仍original/legacy。每个真实采样旁路绑定财务operation、
用量和UTC起止；sample-start/return、原Token、候选与独立判据一并seal。
含optimizer的checkpoint和sampler分开保存，确认receipt/journal才推进训练状态。
未知optimizer不重放，确认过的update/rollout可显式恢复；最多两次已知恢复。

所有训练调用仍由旧TrainerV2、normalized_batch等数值内核执行；按既有组件archive
和实际权重重核Mask/reduction/恢复容差证据。新初始LoRA需实际下载验证B为零、
结构与冻结baseline相同；只读下载验证归属、TLS和大小限制，不记录签名URL。

完整资金预留按全部24组有更新的上界计算，不假设零信号省额度；新增预算不充值、
不使用重置卡。24条开发未过门槛时剩余192条不运行。样本surprisal只作代理，
完整熵仍未测；所有失败消费保留。统计为单seed/3个已见家族探索性结果。

## 真实开发结果：按门槛停止

真实进程正常exit0，启动到24条结束共664.92秒（含预检、初始化、下载、反馈复核与
容器，不是GPU训练时间）。24/24完成，245次真实采样，669,604输入+输出Token；
未知provider调用0，财务pending0，实际optimizer update为0。

| 算法组 | 题数 | 独立成功/声明轨迹 | 混合G4组/声明组 | 有证据的反馈后修复成功 |
| --- | ---: | ---: | ---: | ---: |
| match_rank / path_router | 2 | 0/8 | 0/2 | 0 |
| dependency_batches | 2 | 3/8 | 2/2 | 3 |
| table_join | 2 | 4/8 | 0/2 | 0 |
| 合计 | 6 | **7/24** | **2/6** | **3，来自1个算法组** |

冻结组奖励如下，不能按家族合并后重新计算Advantage：

- dev_00 / router_04：[0,0,0,0]
- dev_01 / router_05：[0,0,0,0]
- dev_02 / dependency_04：[1,0,0,1]
- dev_03 / dependency_05：[0,0,1,0]
- dev_04 / join_04：[1,1,1,1]
- dev_05 / join_05：[0,0,0,0]

准入检查：成功数7处于6–18/24范围，通过；至少3混合组实际2，失败；混合组覆盖
至少2算法组实际1，失败；真实反馈失败后修复覆盖至少2算法组实际1，失败。
表连接家族平均4/8不能替代逐实例G4变化，其两个组分别全成功/全失败，均无相对
成功奖励信号。三条依赖成功有独立partial check和后续真实采样/新Patch支持反馈恢复，
不把所有7次成功都称为交互学习或反馈收益。

因此curve-state终态dev_failed；96条训练、96条验证均保留为not_run，科学队列
实际24/216。0/8/16/24验证成功率为null/未测，没有训练曲线或Base/RL效果比较。
本轮不追加采样、挑任务或改阈值进入训练，不换算法。当前唯一结论是本版数据与
固定配置通过判据检查，开发训练信号/反馈覆盖仍不足；不能推断GRPO无效。

## 数值、财务与安全

实际初始adapter下载核验通过：结构/配置与冻结baseline一致、B矩阵范数为零；
保存含optimizer的初始状态与sampler。历史Mask/reduction/恢复证据按原内核hash
复核，不冒充本轮新optimizer更新或最终恢复实测。无训练，因此无新fresh-policy
clipped share或完整策略熵测量。样本surprisal只作采样代理，不能充当完整熵。

完整216条准入上界58.866元，新增阶段上限100、累计150；实际新增保守占用
**1.626元**，累计保守占用**50.589元**，阶段剩余额度98.374元，无pending。
这些是保守估计及预留，不是项目精确实付账单；未充值、未使用重置卡。
旧48.963元原样结转，旧账本字节不变，旧operation ID继续拒绝重放。

账本收尾增加单一子epoch封存：同一旧父账本不能迁移第二个新目录重新领取额度，
旧CalibratedBudget入口对已迁移父账本拒绝写入。该补强在所有24条结束后执行，
只新增独立claim文件，不改实际账本金额、旧判据或轨迹。原执行源码与补强源码
分别保存在run-01/source.tar.gz及post-execution-budget-hardening.tar.gz；
差异只涉及curve_budget.py/calibrated_budget.py，无重新采样/训练或追认新版本实测。

security-before与security-after分别执行真实容器攻击探针：无宿主挂载、禁网、
cap drop ALL、host canary隔离且不变、超时/取消后子容器移除，配置中的实际凭据
文本检测通过。安全结论限这些探针和扫描对象，不声称全部攻击面或所有Secret零泄漏。
初始远端权重与恢复状态保留，未删除资源；长期存储和服务最终账单未归属核验。

## 证据、复核与后续

本地根目录`benchmark-results/trainable_agent/learning-curve-v2/`：

- audit-01：420项新判据实测与执行源码；[公开摘要](../results/grpo-learning-curve-v2-coverage-2026-11-08.json)。
- run-01：plan、配置、source archive、初始权重审计、24份sealed轨迹、原始Token/
  logprob/财务ID/UTC、候选、Trace、独立verdict与partial feedback proofs。
- run-01/finance：迁移、历史carry与唯一新活动账本；旧parent hash及operation继续保留。
- analysis-01.json及analysis-recheck-01.json：逐字段相同的离线派生，不是两次模型运行；
  [可提交完整指标](../results/grpo-learning-curve-v2-development-2026-11-08.json)。
- execution-01-started/ended.json：真实进程起止、退出码与脱敏日志。
- security-before/after：两轮真实安全门禁；未使用假adapter替代真实副作用检查。
- swanlab-development：仅开发数值与封闭配置上传并API回读，项目PRIVATE，标记
  train/updates=0、rollout/completed=24/declared=216、eval=7/24；未上传Prompt、
  原始Token、代码、私有判据或checkpoint URI。

[私有SwanLab开发记录](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/sj7yta17)。

离线重算使用新输出，不连接provider、不改原始文件：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.curve_analysis \
  --directory benchmark-results/trainable_agent/learning-curve-v2/run-01 \
  --output benchmark-results/trainable_agent/learning-curve-v2/analysis-new.json
```

当前Goal按开发门槛失败交付，不启动下一版本。下一候选是独立开发材料上的逐实例
难度/缺陷组合审查；不能以家族平均成功率代替G4信号标定，不能在本队列补采样或
重用已打开validation做难度筛选。扩大模型/预算或换算法均需另立具体协议。

## 覆盖矩阵

| 能力 | 证据 | 状态与边界 |
| --- | --- | --- |
| v2尾斜杠判据、reference/错误解 | audit-01，420/420 | 有限探针全部通过，v1失败不变 |
| 同族划分、原始Token、逐条财务绑定 | plan/学习binding/financial-calls | 24条开发通过；共享reference与缺陷原语，非新算法泛化 |
| 初始LoRA零B与结构 | initial-sdk/curve-initial-audit.json | 真实检查通过，未进行新更新 |
| 显式150/100迁移、历史/未知调用保护 | finance与预算契约 | 真实迁移，no pending；单子epoch补强为收尾后的契约验证 |
| 24条开发准入 | readiness.json与反馈proof | 混合数量/家族、反馈家族门槛失败 |
| 216条调度及四检查点、确认更新恢复 | test_curve_execution | 仅确定性contract_fixture，不算provider训练/评测 |
| 24槽训练、96条验证、学习效果 | 无 | 未运行/未测，不能报告0%或曲线改善 |
| 子进程/网络/Secret副作用探针 | security-before/after | 本轮所列真实探针通过 |
| 自动未知RPC重放 | 契约拒绝 | 禁止；没有实测未知provider更新后续训 |

## 最终检查

最终root Ruff format/check、175文件strict mypy与pytest1072通过；实验包168文件
strict mypy与pytest299通过。24条开发均实际执行功能测试，无静态策略拒绝代替
本轮模型功能判定；420条数据探针另计。完整声明216，完成24，未运行192。
离线报告逐字段复算一致；运行前后旧活动父账本hash一致。收尾安全扫描33531份
受检文件，实际配置凭据文本命中0；本实验无残留运行容器。私有SwanLab状态
FINISHED仅表示开发指标上传结束，不表示训练完成或通过学习门槛。

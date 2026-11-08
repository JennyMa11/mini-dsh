# 新任务离线调度：固定队列、完整准入与确认后恢复

日期：2026-11-07；分支`experiment`；范围依据[ADR0044](../adr/0044-learning-orchestration.md)。
本阶段完成实验控制器的离线接口与故障验收，不连接模型服务，不训练，不修改旧实验。
目标“交互学习收益，再比较RL算法”仍未完成；控制结果不计为模型成功率。

## 1. 已准备的队列

`learning_schedule.py`生成完整80条队列，固定case、版本、算法组、判据摘要、顺序、
seed及完整资源分配。源码清单与字节摘要同时冻结：文件变化、新增、删除均使计划失效。
`source.tar.gz`归档核心/实验Python源码、两套配置和锁文件；它不是干净Git提交，
不包含测试/文档和完整机器环境，不能据此宣称任意机器可重放。

| 阶段 | 固定数量 | 策略与用途 |
| --- | ---: | --- |
| readiness | 4个名义train家族的variant0 × G4 = 16条 | 同一initial policy；不并入梯度 |
| train | 8个train任务 × G4 = 32条 | seed17打乱任务顺序；组内snapshot固定，组间允许更新 |
| test | 8题 × 2次重复 × Base/RL = 32条 | 每对同seed，顺序交替；Base为initial，RL为最后确认checkpoint |

数据为`learnability_dataset_v2`，router/media同属train的`match_rank`。
4个名义train家族只有3个算法组，8题的一半来自match_rank；test为4个算法组。
这种偏斜保持可见，不把名义家族当作独立算法样本。

readiness要求至少2个G4混合Reward、总成功4–12/16，且反馈恢复覆盖至少2个不同
`family_group`。反馈路径须依据真实Trace和独立部分候选检查，不能用最终成功推定。
未过则保留原80行，后64行保持not_run。这是研究可行性条件，不是显著性检验，
也不替代旧H1–H3门槛。

训练仍为独立0/1成功Reward、G4、A=Reward−组均值。全同Reward组写skipped和不变
checkpoint回执，不补样本、不提交optimizer。固定最后checkpoint后才评测。
实际模型、LoRA/采样配置、数值guard与远端身份尚须后续适配阶段冻结、验证；
旧函数任务的SDK验收不能自动转移到新任务。

## 2. 接口与恢复

新增代码全部在独立实验包，核心Loop和插件架构不变：

| 文件/接口 | 输入 | 输出与职责 |
| --- | --- | --- |
| `learning_schedule.py` / `prepare` | 新目录、data evidence、execution kind | 本地plan/source archive；不创建SDK客户端 |
| `learning_execution.py` / `execute` | plan、ResourcePort、RuntimePort factory、可选resume | 单写者控制状态、行级artifact绑定、Group/UpdateReceipt |
| `RuntimePort` | initialize/restore/rollout/feedback/update的显式参数 | 后续提供真实实现；当前只有测试假后端 |
| `ResourcePort` | available/committed/pending | 首次factory及后续步骤前检查完整剩余队列 |
| `learning_prepare.py` / `admission` | 冻结目录、只读账本 | 资源能力、缺口、行数与hash；execution_admitted始终false |

prepared/running/readiness_failed等是外层实验状态，不修改Agent Loop终态。
外部操作前先保存pending，恢复先检查计划、已确认输入及回执，再考虑打开runtime：

1. 已封存rollout可复用；未返回或缺失完整证据的采样不自动重放。
2. 已准备Group但尚未提交更新，可复用原Group和轨迹，不重采样改变组。
3. 回执绑定Group、Datum hash、journal、before/after checkpoint。有效更新必须确认
   optimizer state及配套sampler；零信号必须skipped且checkpoint不变。
4. 回执已写、控制状态未推进时，接受原回执后恢复，不再次提交同一梯度。
5. optimizer未知且无完整回执，在factory前拒绝，不把异常当成“未执行”。
6. readiness结果及输入hash持久化；确认后的恢复不重复反馈检查。
7. 完成后再次调用只读返回；已确认artifact变化或第二writer进入则拒绝。

feedback应为本地证据分析/独立容器检查，不发模型请求。真实调用方仍须持活动财务
租约、逐RPC预留和结算；只读ResourceSnapshot不能代替消费控制器。当前没有真实
provider执行CLI或SDK factory。本地临时文件、fsync和原子发布的验收覆盖进程故障，
不覆盖整机断电/文件系统损坏，也不能认证任意外部文件的远端来源。

## 3. 实际离线验收与证据范围

定向15项通过，JUnit及输出保存在本地
`benchmark-results/trainable_agent/learning-orchestration-v1/contract-suite-01.*`。
后端全部标记contract_fixture，Token/logprob、Reward、checkpoint人为构造，
不证明真实模型表现、独立任务正确性或SDK optimizer恢复。

| 覆盖项 | 触发与禁止行为 | 证据 |
| --- | --- | --- |
| 固定队列 | 80行、8个G4、test同seed交替；不删失败/未运行行 | schedule/full control断言 |
| 完整预算 | 可用7.333或pending；禁止打开factory | factory计数为0 |
| readiness失败 | 假后端全部失败；禁止训练/test | 16完成、64 not_run、0更新 |
| 确认后恢复 | 回执写完后注入崩溃；禁止重放梯度/反馈 | 恢复80行，合计8次更新、restores=1 |
| 未知optimizer | optim_submitted后抛错且无回执 | pending保留，replacement open/restore/update均0 |
| Group准备后崩溃 | group.json存在、update pending尚未保存 | 原80条仅采一次，恢复完成 |
| 零信号 | train组全0；禁止optimizer | 8个skipped，checkpoint保持initial，80行保留 |
| 输入/单写者 | 已确认journal/轨迹变更，或第二writer | 后续执行拒绝 |
| 源码库存 | 模拟冻结后新增/删除源码 | 回归先复现缺口，完整map比较修复后通过 |
| 只读报告 | 模拟资金不足/充足、缺少一行的state | 执行始终不准入、账本不变、丢行被拒绝 |

full-control和artifact-change用例最后故意破坏文件，inspection-state用例故意少一行；
这些最终目录是负例，不能当作有效研究运行。pytest的*current目录别名不重复计数。

实际活动账本另有只读准入和factory阻断证据：

- `prepared-01/plan.json`：冻结80条，真实provider任务完成0条。
- `admission-01.json`：required=20.302、available=7.333、shortfall=12.969，执行未准入。
- `factory-block-01.json`：factory_calls=0；账本前后SHA均为
  `7e9712a184726e9f0013a3b28c5ebc5faee19bc4384f181366292dee8ce2dc7c`。

零调用依据是无真实适配器的入口与代码路径检查，不是提供商账单审计。
本阶段没有充值、重置卡、GPU租用或新云端上传；历史模型结果保持不变。

## 4. 完整资源分配与停止条件

| 项目 | 保守分配 | 依据与限制 |
| --- | ---: | --- |
| 80条采样 | 7.200元 | 沿用0.09元/run完整预算规则，不是平均实付预测 |
| 最多8组训练 | 10.712元 | 每组1.339；G4×12轮×6143位置的padding计价上界 |
| 初始化、optim及快照 | 1.890元 | client一次、optim八次、state/sampler各最多九次；管理额度不是已证实收费 |
| 额外准备/恢复 | 0.500元 | 硬额度；最多两次恢复各预留0.01，不借评测资金 |
| 合计 | **20.302元** | 按最坏路径分配，不提前支出 |

当前保守累计42.667、可用7.333元，上限50元、pending=0。加入完整分配将为62.969元，
缺口12.969元；算式不是提额授权或实际消费。早期19.702元草案少考虑逐有效组的
optimizer恢复保存，作为历史保留，不继续用于准入。

未来真实采样前还须核验报价及新的数值准备是否装入0.500元，不能保证所有准备均可
完成。完整余量不足、未知RPC、输入变化、预算违规或readiness未过均停止，不先跑
部分队列再要求追加资金。没有新服务性能测量，不承诺墙钟时间或GPU显存。

## 5. 复核和下一阶段

本阶段完整回归：主项目1072项（51.64秒）、实验包225项（19.85秒）通过；Ruff格式/
静态检查508文件通过，strict mypy主项目175文件、实验包148文件通过。命令、退出码、
原始输出及JUnit保存为本地quality-gates-01，不把这些数量当作模型/安全功能验收。

离线契约可单独复核，不需模型服务：

```bash
uv run --project experiments/trainable_agent --extra lab pytest -q \
  experiments/trainable_agent/tests/test_learning_schedule.py \
  experiments/trainable_agent/tests/test_learning_execution.py \
  experiments/trainable_agent/tests/test_learning_prepare.py
```

只读复核已准备目录，不覆盖证据、不启动模型：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_prepare \
  --output benchmark-results/trainable_agent/learning-orchestration-v1/prepared-01 \
  --admission-only
```

准备新草案时使用全新目录并去掉--admission-only。源码变化须新目录和新归档，
不得改写旧hash。测试和质量结果另存本地quality-gates与最终manifest，不能据数量
宣称完整功能覆盖。

下一阶段须单独登记并完成具体PyTRIO适配：实际sampler/snapshot/tokenizer绑定、
有锁账本、真实更新/保存/恢复、可审计反馈检查及最终配对指标汇总。先做可注入离线
契约，形成可审查的完整方案；真实数值Smoke/readiness/训练仍受资金及用户实验指令
约束。当前不宣称SDK恢复、交互可学习性或算法收益已验收。

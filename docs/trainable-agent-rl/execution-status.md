# ¥50核心实验执行状态

> **继续 RL 实验先读：[近期工作总结与启动手册](research-handoff.md)（2026-11-09）。** 汇总两条实验主线、最新停点、候选策略误拒、正控待接线与离线复核入口；以下历史“最新”段落按各自时点理解。

> **最新完整结果（2026-11-09）**：[v4 单 seed 学习曲线](learning-curve-v4-recovery.md)：
> 192/192 完成，96 训练＋96 验证，17 次真实更新。检查点成功数为 7/24→8/24→6/24→8/24；
> 最终 +4.17 个百分点，探索门槛通过，95% 家族聚类区间 [−37.5,+25.0] 个百分点，
> 稳定收益未证实。依赖/表连接各 +2，路由 −3；原初始化失败保留，真实等价新建起点
> 与最终权重变化已核验。以下为各历史时点记录。

> **最新 v4（2026-11-09）**：[固定学习曲线与初始化停点](learning-curve-v4.md)：
> 24 道 train/validation 的 336 项数据检查通过，192 行队列已冻结，复用 v3 准入。
> 真实运行在初始 optimizer checkpoint 保存时遇到 ActorEventSubmitError；只读核查
> 恢复任务 ENDED、0 checkpoint。未产生采样或更新，学习效果未测。
> 新增保守占用 0.160，累计 52.264，含 0.100 未知请求 pending；未重试或释放。
> 以下各段保留各历史版本时点，不代表当前余额或运行状态。

> **最新开发v3（2026-11-08）**：[公开反馈修订与开发准入](development-feedback-v3.md)：
> 保留6题原缺陷/私有判据，仅修订公开反馈，84项容器检查通过；24/24真实开发
> 成功8/24，混合组4/6覆盖2算法组，反馈修复3条覆盖2算法组，原准入通过。
> 252次采样、699368 Token，训练/新训练客户端/新checkpoint均0；未自动训练。
> 新增保守占用1.515，累计52.104，无pending；唯一活动财务账本不变。

> **最新v2（2026-11-08）**：[判据复验与真实开发诊断](learning-curve-v2.md)：
> 420/420判据通过；dev24/24、成功7/24，但混合组2/6且仅1算法组、反馈修复也仅
> 1算法组，原门槛未过。后续96训练/96验证未运行，实际更新0；245次采样、669604 Token。
> 已显式迁移累计150/新增100档，保守占用50.589、阶段新增1.626，无pending；
> 旧账本封存，新唯一活动账本为learning-curve-v2/run-01/finance/budget.json。

> **最新阶段（2026-11-08）**：[单 seed 学习曲线判据门禁](learning-curve-results.md)
> 完成30题/420项真实容器检查；私有判据漏检4个尾斜杠单缺陷实例，门槛失败。
> 后续216条模型队列未运行，未训练、未迁移预算；新增100元/累计150元为已确认
> 但尚未落地的后续资金契约，原活动账本不变。本版按协议交付停点报告。

> **最新后续实测（2026-11-08）**：[固定示例的一步 GRPO](example-grpo-step.md)完成
> 1 次真实更新和 8/8 新配对评测。初始参数差为 0、最终差约 0.0425；原判据
> Base/RL 均 0/4。增加 issubset 的独立影子判据均 1/4，原分数不改，未观察到收益。
> 新增保守估计 2.126 元（含恢复），累计占用 48.963 元、可用 1.037 元、无 pending。
> checkpoint、完整轨迹和财务证据已保存，私有 SwanLab 已回读。下文均为历史快照。

> **最新真实实验（2026-11-08）**：[反馈示例诊断](feedback-example-pilot.md)已完成
> 16/16：原提示 0/8，示例 3/8，混合奖励组 0/2 对 2/2。未更新模型权重；仅为
> 两道开发题的提示对照，不证明 RL 收益。新增保守估计 0.922 元，当前累计占用
> 48.157 元、可用 1.843 元、无 pending。原始轨迹和财务逐调用证据已保存，数值
> 结果已上传私有 SwanLab 并回读。下文金额为各自历史快照。

> **最新复核（2026-11-08）**：[交互可学习性 GRPO 完整结果](learnability-grpo-results.md)：
> 80/80 完成，3 次有效更新，配对测试 Base 7/16、RL 4/16，未观察到收益。当前混合
> 保守占用 47.235 元，无 pending，实付尚未核验。最终权重参数变化已实际确认，
> 完整数值结果已上传私有 SwanLab 并回读。本次未启动新实验；下文保留各阶段快照。

> [学习与算法准入复核](algorithm-comparison-gate-review.md)：原正向学习门槛未过，
> 算法收益对照不准入。32 条训练轨迹的离线数学检查确认：固定五个全零组在两种
> loss 下均无策略梯度；当前 native/GSPO 还同时改变长度权重。账单界面仍不可读，
> 现有额度不足此前完整对照的准入预留，不自动换算法或启动新付费队列。

> **当前状态（2026-11-07）**：H1–H3及离线反馈审计已结束；最新
> [闭合账期覆盖](feedback-hour-reconciliation.md)后保守占用42.667元、可用7.333元，
> 无pending、累计上限仍50元。[新家族夹具](learning-fixtures-results.md)已完成224次
> 离线容器检查；划分v2有3个train算法组/4个test组。[轨迹桥接](learning-bridge-results.md)
> 已通过离线契约和脚本控制。[离线调度](learning-orchestration-results.md)固定80条，
> 用contract fixture验证确认后恢复、零信号跳过和未知更新拒绝重放。
> [具体SDK适配](learning-provider-results.md)已用假客户端及脚本容器验收，真实身份/数值未测，
> 尚无新模型结果。[固定执行入口](learning-execution-results.md)已离线验收，历史数值组件
> 按原权重和源码复核，新增初始LoRA门禁尚未真实触发。完整20.302元、缺口12.969元，
> run未准入，继续不启动采样/训练。
> 下文保留历史时点，不将旧余额当作当前可用金额；目标及算法比较仍未完成。

> 2026-11-07 新增：[Lab GSPO 与 SwanLab 轻量迁移](lab-migration.md)，当前阶段为数值探针和2组工程Smoke；累计仍¥50，新增上限¥5。历史正式结果保持原样。

> **本轮已完成（2026-11-07）**：[40槽及完整配对test结果](../results/trainable-agent-expansion-2026-11-07.md)。保守累计¥41.163（含¥2预留），实付账单待对齐；旧状态段落按其日期保留，更高预算档未启用。

日期：2026-11-06。目标：完成[¥50核心协议](lightweight-experiment.md)。状态：**进行中，S0部分完成，尚未完成实验**。当前已完成一次文本采样与一次Logprob重算的SDK探针；尚未提交训练前后向或优化更新，没有用离线结果替代真实RL结果。

## 已完成及证据

- 独立实验项目 `experiments/trainable_agent/`，Python3.12、uv锁定PyTRIO0.2.9；未修改生产AgentLoop。
- 原始Token右移对齐、仅本轮assistant动作mask、group-centered advantage、无信号组、在途费用预留与实际超额保留的契约实现。
- 40道候选任务按家族分为train16/validation8/test16，参考解与固定输入输出判据位于控制面；尚未冻结科学队列。
- 80次真实Docker检查：每题原始bug失败、参考解通过；输出在 `benchmark-results/trainable_agent/task-gates/summary.json`。另有提前exit(0)探针，验证仅退出码成功不能获得判据通过；原始结果在 `early-exit-probe/`。
- Docker使用原生arm64、禁网、1CPU/256MB、无宿主挂载的现有Sandbox；此轮结束没有遗留实验容器。通用Secret/网络/取消攻击矩阵仍需逐项执行，不能由这些检查补认全部安全项。
- PyTRIO已保存凭据可用于只读访问，账户可用Qwen/Qwen3.5-4B训练及采样。公开价格/单位转换摘要见[service-preflight.json](service-preflight.json)：输入1.49、缓存输入0.30、输出4.54、训练4.54元/百万Token。按轻量协议平均Token情景，核心计算约5.16912元，未含保存、重算与计费取整余量；运行前仍需核对计费及在途费用边界。
- 原始只读metadata及官方页面bundle在 `benchmark-results/trainable_agent/preflight/`；代码/锁文件SHA摘要在 `implementation-snapshot.json`。这些是阶段快照，不是最终训练源快照。

## 验证记录

本轮根项目格式与ruff通过，`uv run mypy src tests`通过（175文件），架构检查112模块、分层违规和显式环均为零。`uv run pytest -q`最终1072 passed（49.55秒）。实验包单独pytest为11 passed，strict mypy覆盖10个源码/测试文件并通过；命令见[实验包README](../../experiments/trainable_agent/README.md)。

第一次全量回归发现旧规划文字“32次测试集任务运行”的旧措辞被文档数量检查器误识别为工程测试总数；已改为“32次测试集任务运行”，再次完整回归通过。没有改写历史评测数字。

## 尚未通过的S0门禁

1. 候选任务目前是小函数变体，必须补齐真实多文件依赖、异常输入/输入不变性、错误解和近重复检查，构建4道独立工程夹具；不因现有固定输入通过就承认整个数据集验收。
2. Recording Model adapter、原始模板/tokenizer身份、真实逐Token概率语义、非法工具Observation及持久化完整性。
3. 全局持久费用账本、每RPC准入/取整/在途预留、真实训练reduction与Mask数值验证。
4. Train checkpoint恢复、RPC结果未知处理、故障注入、安全探针与完整源码冻结。
5. 最多16条真实Smoke，依据实际消耗确认完整主队列在50元内。

只有这些S0门禁完成后才开始80条主训练和Base/RL验证及测试；不得跳过S0直接跑20槽，也不自动增加100元版的第二奖励臂。

## 下一次工作入口

先阅读本文件、实验包README及当前代码；优先补任务/验证覆盖，再实现可注入的Recording采样adapter和其契约测试。真实费率已经核验，不必再根据旧的“价格未知”假设停工；仍须将实际报价与费用准入接入运行器。最终报告必须包含训练/测试结果、完整分母、原始证据与实际支出，当前这些结果尚不存在。

## 第二次实施推进：真实SDK探针与持久化（2026-11-06）

前一次目标回合有实际代码、容器与预检证据进展，本回合继续S0，没有缩减最终176条核心队列。

- 新增 `model.py`：单run固定policy身份、真实请求渲染、Token预算、原始sample旁路、严格工具envelope解析。非法动作返回recoverable error，不隐藏重采样；旁路写失败时不交付可执行动作。
- 新增 `budget.py`：复用平台FileLease，原子保存费用预留/结算；重启保留未知RPC费用，重复operation拒绝，限额不能被重启改高。费率按各类别向上取整到分，无缓存折扣；这是一种保守费用记账，不等同后台实际扣款。
- 新增 `pytrio_backend.py`：显式注入SDK sampler/tokenizer/预算；使用原生工具模板并关闭thinking。实际tokenizer检查发现Transformers默认返回BatchEncoding，已显式指定return_dict=False并增加回归契约。
- 新增 `recording.py`：0600原始Token旁路，经fsync后原子发布；重复call不能覆盖已有记录。
- `public_fixture`新增双文件导出：solution.py入口依赖implementation.py，另含一个可见失败复现与规格README。参考解和剩余隐藏判据不进入工作区。新版80次bug/reference容器验证通过，证据 `benchmark-results/trainable_agent/task-gates-v2/summary.json`。这证明跨文件定位存在，仍不代表复杂多文件修复能力。
- 新增故障契约包括：RPC返回用量错位/超时保留费用、原始Token写入失败、工具协议歧义、双writer锁及重启重复提交阻断；实验包目前25项检查通过，strict mypy覆盖19个文件。原项目源码本回合未改。

真实探针入口为 `python -m minidsh_rl.probe`，输出目录已完成为 `benchmark-results/trainable_agent/sdk-probe-01`，不得重用或重启该已完成探针。生成2个Token，sample与同一冻结sampler重算的最大概率误差0.00006160291377454996，小于预声明0.001；结果通过。该文本探针不执行任何工具动作，不是一次Coding rollout或RL性能证据。它属于Smoke准备开销，不替代16条Smoke rollout。

全局唯一费用账本：`benchmark-results/trainable_agent/core-budget.json`，limit=50，保守已计0.04元，无pending operation。金额是本地无缓存/逐项取整上界，尚未完成后台账单对账；禁止创建新账本重置累计消费。实际tokenizer/词表和模板摘要在 `preflight/tokenizer.json`；模板SHA-256为 `a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715`。

下一步仍须完成：原Loop真实工具回合接线与trace/sidecar关联、候选限制与异常/不变性/错误解探针、独立4题工程夹具、训练Mask/reduction数值检查、Train恢复与RPC故障处理。之后执行16条Smoke，再开始80条主训练及16条validation/64条test，不以SDK文本探针替代任何科学队列行。

## 本回合最终状态：Loop接线与真实校准（2026-11-06）

新增 `rollout.py`，通过原AgentLoop/ToolRuntime在禁网容器执行，只导出公开fixture，结束后校验除implementation.py外所有文件不变，在新容器执行独立判据。脚本模型的完整读改测与篡改可见测试拒绝两项真实容器契约已通过；这些属于Component/Trajectory证据，不是模型性能数字。发现并修复了docker cp保留宿主UID导致去特权容器内文件不可写的问题：仅在实验层改为由容器用户创建fixture，不放宽权限、不改生产Sandbox。

新增4题独立工程夹具，8次bug/reference容器检查通过（`smoke-task-gates/summary.json`）。此时研究集仍为40题，工程题不进入主实验统计。异常输入/不变性、近重复及更充分错误解覆盖仍待补，不能因已有门禁绿灯省略。

### 三轮校准的完整事实

| 队列 | 变更与结果 | 结论 |
| --- | --- | --- |
| calibration-01 | 8条尝试，0条成功；XML原生工具被JSON解析器拒绝，随后mid-system模板报错 | adapter缺陷；不能归因为模型修复能力 |
| calibration-02 | 8条尝试，0条成功；原生协议修复后仍遇到绝对cwd拒绝、不适用编辑命令和重复读取，全部到步骤预算 | Harness指引不足；失败完整保留 |
| calibration-03 | 工程Prompt补齐允许程序、相对cwd和python argv编辑示例；8条完成、8条独立验证，6条成功 | 多轮工具链可用，尚未证明RL收益 |

第三轮消耗74608输入+输出Token、60次模型调用、52次工具执行，3次模型输出格式拒绝。工具执行事件均标succeeded仅表示执行器完成，命令非零退出另按现有指标计分；不能据此宣称工具语义100%正确。6条正确中有1条Loop终态budget_exhausted，其余5条finished；另2条budget_exhausted且候选不正确。任务正确性与终态分别保存。

XML原生协议解析及中途system反馈转换（保留初始system约束，将运行时反馈渲染为user输入）已有回归；原始生成Token与概率不改写。实际Prompt/Token和变更源码hash分别保留在各队列policy/source/tokens/trace中。主训练及test未开始，未用测试结果挑配置。

范围追加已写入主计划：保留前16条失败，增加第三轮工程校准，S0最多32条=24校准+8训练Smoke，核心最多192条=32+80主训练+80验证测试，费用仍≤50元，Smoke仍≤10元。没有增加Reward对照臂。第三轮Prompt会作为后续Base/RL共用的冻结Harness版本。

全局费用账本当前保守累计**2.68元**，pending为空；其中已包括0.04元SDK探针。为完整最终评测保留15元可用额度（在采样adapter准入中执行），不是额外预算。保存原费用上限50，不重建/重置账本。后台账单对账尚未完成，这个数不是已确认实际扣款。

当前实验包30项检查通过、strict mypy覆盖22个文件，根项目格式/ruff、175文件strict mypy通过；本回合原项目完整pytest仍为1072 passed（49.47秒），原112模块架构门禁无违规/环。各次容器已清理。

### 下一步：进入训练端S0

优先实现 `datum.py`、`trainer.py`、训练事务/Train state恢复契约与实际reduction/Mask数值检查，全部通过后执行剩余2槽×1题×4=8条训练Smoke。工程Smoke候选选择last_even与defaults，依据工程校准显示两者成败混合，不用于主科学结论；提前声明后不补采样凑成功。未通过训练门禁前，不开始20槽主训练。

后续必须复用 `core-budget.json`，保留三个已终止校准队列，不重启这些已完成进程。当前无待观察的训练或采样进程；校准进程80756已exit0。目标仍未完成：真实LoRA更新、checkpoint恢复、主训练、Base/RL配对评测和最终账单/报告均未交付。

## 训练完成、固定配对评测进行中（2026-11-06）

本节更新前文“尚未实现”的历史状态；早期失败探针均保留，没有重置账本或删除失败。原Runtime、AgentLoop和插件依赖方向未因实验改动。

### S0数值与安全门禁

- `training-probe-01`：LR=0被服务明确HTTP 400拒绝；没有将未知请求自动重试。`training-probe-02`：服务不返回假设中的grad_norm，调用确认后本地KeyError，保留结果。`training-probe-03`：已开始的client不允许load_state；后续使用官方从Train state新建client接口。
- `training-probe-04`通过实际BF16 LoRA权重差检验：零Mask更新为0，复制样本后更新比0.99987，将单段分成两段后更新比0.50655。由此判定当前后端为sample_mean，实验层按N_datums/N_assistant_tokens补偿，不假设为Token mean。BF16解码失败后只继续已确认请求的本地权重分析，没有重复提交未知RPC。
- sample与同sampler重算最大logprob差0.0000616，通过原0.001门槛；训练引擎与采样引擎差异最大0.01575，**未通过原跨引擎0.001假设**。调查发现p/q范围0.98438–1.00608、均在PPO clip内；已在主计划记录修订：保留真实采样q，不把不同引擎误差当作bitwise相同；每组训练前检查非零Advantage Token，超过5%落在clip区间外即停止，不据test调阈值。
- `resume-probe-01`先进行实际Adam更新，再保存含optimizer的Train state，比较恢复后的下一步；相对更新差0.02738，小于提前声明的0.03。属于容差内恢复，不是位级确定性证明。
- `smoke-train-01`固定2槽×4=8条：last_even奖励[1,0,1,0]产生一次真实更新；defaults为[1,1,1,1]，零Advantage整组跳过。S0结束累计保守费用6.09元。
- `task-gates-v3`在主训练前补齐异常输入、边界与不变性检查，40题bug/reference共80项容器判据通过；纯函数AST限制拒绝import、I/O、introspection等候选，不以此替代OS隔离。
- `security-gates-02`实际容器探针通过：无宿主挂载、网络none、capabilities全丢弃、宿主canary不可见且不变、超时/取消后的子容器移除；已配置实际凭据文本检测器，1130份受检artifact文件无命中。结论仅限本次检查对象，不宣称系统全域零泄漏。

### 主训练事实

`core-train-01`完整20槽×4=80条，没有删掉失败或补采样凑分组；16条train任务按seed=17固定洗牌，再重复前4条。独立验证通过60/80仅是训练过程表现，不是泛化效果。8组产生非零更新，12组零Advantage跳过；推理消耗685858输入+输出Token。

冻结信息在 `core-protocol-v2/`：40题按family划分16 train/8 validation/16 test；固定源码快照、公开fixture hash、独立判据hash、镜像digest及预算。主训练源码到配对评测期间保持逐文件byte相同；只新增离线分析模块。实际LoRA r=16、alpha=32、dropout=0，target_modules见 `lora-config-observed.json`，没有假设服务只适配q/v。

全部20组从原始Token重建Datum并核对journal hash、独立Reward、group-centered Advantage、组内固定policy及消耗，`training-audit.json`全部通过。最终Train state为 `trio://run_kkfuzsulr02j/weights/final_state`；sampler权重索引见主训练 `final_weights.json`。训练完成累计保守费用20.68元，包含全部S0与失败工程探针。

### 配对评测协议

`core-eval-01`固定80条：validation 8题×2策略=16，test 16题×2重复×2策略=64。两策略使用同一Harness、Prompt、工具、预算、独立验证器及每对相同seed，顺序逐对交替。Base为主训练初始零LoRA snapshot，RL为最终snapshot；不按validation挑checkpoint。每条使用新禁网工作区，结束后再在新容器独立验证，完整分母保留。分析工具在运行期间新增，不修改训练/评测源，也不根据结果改配置。

当前主训练已结束，配对评测进程仍在运行。最终结果、费用结算和门禁以最终报告为准；不能据本节阶段进展宣称任务已完成或RL已获益。

## 核心版执行完成（2026-11-06 22:03后）

最终80/80配对评测已完成，进程正常exit0，不重启队列。Test Base 25/32、RL 24/32；差−3.125个百分点，4家族cluster bootstrap探索性95%区间[−18.75,+9.375]个百分点。未观察到成功率或单成功Token改善；validation 5/8与6/8单列，不据其挑checkpoint。核心实验结论为链路可运行、收益未证实，不以训练60/80证明泛化。

全局账本累计保守31.38元（S0 6.09、主训练14.59、全部验证/测试10.70），pending为空，limit仍50。后台22:03附近只读刷新可见当日模型Token已入账3.37元、均代金券；余额54.38、现金7.76未变。当前小时可能继续结算、工作空间账单不能精确归属逐run，且当天权重存储尚未单列；不将3.37写成最终实付总额，保留18.62余量。付费训练/采样全部停止。

最终LoRA下载并保留在本地core-artifacts-01，64,952,320 bytes、248个B张量非零，norm=0.13778455。初始临时sampler不在可下载checkpoint列表，第一次下载脚本在只读匹配阶段StopIteration，未产生新的训练或付费操作；后续仅下载已存在的最终权重，没有重新export或重训。最终Train state与sampler URI均保留，未删除原有权重。

最终安全探针通过，受检4288份artifact没有配置中的实际凭据文本命中，超时/取消子容器清理检查通过。全部80条评测原始Token、Trace、队列、候选hash和独立判据核对通过，消耗与费用账本吻合。55项实验检查通过；主项目1072项回归、strict类型及架构检查通过。完整口径、失败例和证据路径见[最终报告](../results/trainable-agent-core-2026-11-06.md)。

离线派生分析03是当前正式口径：01保留原评分器缓存0占位，02将无遥测缓存设为未知，03补充cat重复读取代理；三者来自相同固定运行，不是重测。原始Trace保持不变。没有开始100元Reward消融或新一轮训练。长期远端权重保留存储费不属于本次执行终点后的已结算金额；复用或清理另按实际需求处理。

最终收尾复核：一次完整回归运行中同步修改了ADR索引，既有extension acceptance的工作树冻结检查正确报current_source_changed（1071通过、1失败）；这是收尾操作顺序问题，未改运行中的评测或原始实验数据。停止一切文件修改后重新完整执行，1072 passed（49.51秒）。随后仅补记本说明；结果JSON与完整派生报告hash、冻结训练源码hash及50元账本再次核对一致，本机无遗留运行容器。所有采样/训练/下载进程均已结束。

## 2026-11-07：GSPO / SwanLab 迁移收尾

[工程迁移已完成](lab-migration-results.md)：8条轨迹、2次非零更新，数值缩放与Mask通过；SwanLab私有项目已上传并回读核对。本轮含失败探针保守估算3.036元，累计44.199元，无pending。未执行新算法收益对照，不追加训练；核心Loop与旧GRPO结果保持原样。

## 小仓库交互诊断收尾

[结果](minirepo-results.md)：8题开发集、56项判据检查、64条完整诊断。两档输出上限下single/interactive均1/16；机制门禁未过。新增保守估算3.956元，累计48.155元，无pending，剩余1.845元不足完整Pilot评测预留2.88元。未启动新RL/新test/第三批。

## 2026-11-07：整账期预算覆盖预审

[最新财务预审](closed-period-budget-coverage.md)已刷新JennyMa账单并切回原题目页。
选定时段工作空间模型费用18.97元、已显示存储0.04元；不等于Mini-dsh精确实付。
活动账本仍保守占用42.667元、可用7.333元、无pending。候选测算29.211元尚缺
全6971个操作的覆盖证据，未生效、未创建新epoch或启动实验。前述各阶段金额是
各自历史快照，不能相加或当作当前余额；科学结果仍以各冻结结果文档为准。

## 2026-11-07：确认阶段资金门禁已离线验收

[结果](staged-funding-results.md)：固定80行的新plan可选择按完整Readiness、单训练组、
完整test准入；旧整轮模式继续保留。273项实验包及1072项主项目回归通过，等待、
有界恢复和未知请求拒绝均有假SDK/契约证据。当前Readiness上界2.15元可容纳，
整轮20.302元仍不足；活动账本占用42.667元不变。真实研究行0/80，未新增模型或
训练调用，旧“学习收益未证实”结论不变。没有自动reconcile或启动真实实验。


2026-11-09：用户授权按历史 GRPO 规模进行 Base / GSPO 效果复测；160训练轨迹、
64验证、128测试，当前真实执行中。协议：[冻结协议](gspo-efficacy-v1.md)。
旧test已使用，不称新盲测；不以历史GRPO成绩直接作算法优劣结论。


2026-11-09 Base/GSPO同规模实验已完成：[完整结果](../results/gspo-efficacy-v1-2026-11-09.md)。
352/352真实轨迹、40个G4训练组、25次有效更新；验证10/32→18/32，
测试28/64→33/64（+7.8125pp；95%家族聚类区间[-1.5625,+17.1875]pp）。
单训练seed、既有测试集复测，不能宣称稳定收益或优于GRPO。反传查询失败后按ADR0057
从实际权重与完整Adam状态全等的检查点恢复，原失败保留、124条轨迹未重采；
358项实验回归及前后容器隔离检查通过。不自动扩大新seed或算法对照。

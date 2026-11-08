# 扩展实验实施记录

目标：继续实验，按[扩展方案](expansion-plan.md)先完成A，再按门禁进入P。用户已授权继续执行；当前累计费用上限沿用50元，未启用建议的100/150元档，也未使用含义不明的“重置卡”。本记录区分已实现与未验收，旧实验和账本保留。

## A阶段当前证据

- 只读PyTRIO预检完成：`benchmark-results/trainable_agent/expansion-v2/phase-a/service-preflight.json`。当前返回price version仍为2026-08-21，Qwen3.5-4B输入/缓存/输出/训练单价字段分别149/30/454/454（官方前端换算为分/M）；没有提交采样或训练。
- 新增实验层`cost_model.py`：Decimal连续估价、历史费用与不确定预留、pending/estimated/billed操作状态、显式操作集合账单替换，拒绝重复结算和未完成RPC放行；超额仍计入真实估价并阻止下一调用。它目前是**纯契约，未接入付费准入，未实现持久恢复**。凭据、历史账本和原`budget.py`没有改变。
- 新增`diagnose_formats.py`，对主训练+评测的1066份原始sample重新执行冻结解析器，与83次Trace rejection事件逐run数量一致。68次为`invalid tool parameter JSON`，15次为`one complete tool envelope required`；其中仅4次stop_reason=length，其余79次正常stop。不能据此声称扩单轮输出上限就能解决主要问题。
- 对原样本人工抽查可见JSON字符串中的裸换行、引号/括号不完整，以及同一响应多tool块。这些是实际模型输出问题；当前尚无证据证明解析器误拒合法工具输出。后续优先在开发case验证更稳定的编辑接口/指引，或保留Harness；不偷偷修复JSON后作为原始动作训练。

诊断是旧轨迹离线派生，不是新模型评测；结果含输入/解析器hash，原始文件不改。重算入口（输出选新路径）：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.diagnose_formats \
  --input benchmark-results/trainable_agent/core-train-01 \
  --input benchmark-results/trainable_agent/core-eval-01 \
  --output benchmark-results/trainable_agent/expansion-v2/phase-a/format-diagnosis-recheck.json
```

## 未完成门禁与后续执行顺序

| 门禁 | 当前状态 | 下一动作 |
| --- | --- | --- |
| 当前模型/价格只读核验 | 已完成 | 实际提交前确认报价hash |
| 旧原始格式拒绝分类 | 已完成机械分类，语义意图未全标注 | 24个开发case，冻结唯一候选变更 |
| 精细计费纯契约 | 已实现 | 持久化、锁、崩溃恢复与边界校验，再接入实验backend |
| 账单对齐与旧预留释放 | 未完成 | 新只读账单与账期核对，未证实前不降低31.38历史负担 |
| 新96题及独立判据 | 未完成 | 24家族分割、bug/reference/错误解探针；test不做模型筛查 |
| A的184条与计费校准 | 尚未开始 | 上述门禁完成后逐批执行，A增量上限8元 |
| P的40槽训练与验证 | 尚未开始 | A验收后准入，不能声称已经续训 |

当前没有新的付费采样/训练进程，不把只读服务预检和旧Trace分析计为新rollout。继续目标保持进行中，未宣称完成整个扩展实验。

## 本回合收尾证据

- 实验包65项检查通过（含8项新费用契约、2项轨迹诊断），strict mypy覆盖47文件；主项目1072项回归通过（51.78秒），175文件mypy、ruff format/check、112模块架构检查通过。未在运行冻结测试时修改源码或文档。
- 新只读UI观察：2026-11-06约23:24北京时间，当日模型Token账单合计3.40元（0.20+3.04+0.16），均代金券；余额54.35，其中现金7.76、代金券46.59。与原22:03的3.37快照分开保存，路径`expansion-v2/phase-a/billing-observation-01.json`。旧历史账单不混入，未充值/兑换/重置。
- 新诊断与旧训练源码hash均核对通过，原账本依然31.38/50、pending为空。当前没有新付费模型调用，后续需在持久费用控制与账期归属验收后，才能释放过度历史预留并启动A队列。
- 下一目标保持完整：补齐持久费用控制/校准、新96题、A阶段真实队列，验收后执行40槽Pilot；这次准备进展不等于“继续实验”目标已完成。

## 持久费用控制与校准执行

新增`cost_store.py`：OS锁同时占用v2和原账本，绑定旧账本/报价hash，原账本不写；原31.38负担完整承接，另留2元不确定项。pending/估价/显式账单映射原子保存；磁盘写失败后控制器进入poisoned状态，不允许继续提交。原始checkpoint与实验SDK边界未变。

新增`billing_calibration.py`，固定16次文本请求、每次最多32生成Token，逐次先持久预留，再采样，无自动重试。当前仍按每RPC分级取整的保守价结算，另保存连续估价用于对账；**没有因连续估价较低直接释放旧预留**。整批最多0.32元，并预留后续8元工作额度后才允许创建sampler。已通过超时不重放、价格改变拒绝、账单恢复不双计、旧writer锁及磁盘失败契约；实验包74项检查通过、51文件strict mypy通过。

范围澄清：计费校准单独16次小文本RPC（不算Coding rollout），原规划“嵌入工程采样”改为独立观察批，开销计入原A阶段8元，不增加50元累计上限；A的184条Coding队列仍保持声明分母，不用文本请求替代。运行目录固定`expansion-v2/phase-a/billing-calibration-01`；共享新账本固定`expansion-v2/budget.json`，不可另建预算重置已发生消费。

### 首批真实执行结果

`billing-calibration-01`已完成16/16次实际采样并正常exit0；输入288、输出32 Token，连续报价估价0.0005744元。未通过文本返回评价修复正确性，不计入184条Coding队列。v2账本暂保留0.32元逐请求保守计价，因此承接历史31.38+不确定预留2+新采样0.32=33.70元；pending为0。原v1账本仍31.38，差额不是已扣款，历史不重复计费。

`billing_rule_confirmed=false`：等待相应账期可观察明细，不能因小请求暂未入账即宣布计费语义已确认。现有Service/SDK凭据复用，没有充值或兑换重置卡。下一步继续A的新任务与单变量Harness对照准备，P仍未开始。代码门禁和旧证据hash会在本回合最终核对。

本回合完成复核：实验包74项检查通过、51文件strict mypy通过；原项目1072项检查通过（50.85秒）、175文件strict mypy及架构检查通过；root格式/lint、diff空白检查通过。校准16条Token旁路与summary计数一致、报价hash与新账本一致；旧主训练源码快照及原账本31.38不变。16次校准进程已正常退出，没有正在执行的采样/训练任务。本目标仍进行中，未将小文本校准当作A或Pilot完成。

用户进一步要求“规划后立刻执行到实验完成或额度耗尽，临近耗尽重置”。已经继续实际执行；重置对象仍待澄清，不能把它解释成重置费用账本或无上限充值。预算与安全/独立验证门禁继续生效，接下来按新96题及24开发case对照推进。

## 新任务门禁通过，启动开发对照

新增`tasks_v2.py`与`verification_v2.py`：24家族×4变体，共96题，48 train/16 validation/32 test；家族及case_id与旧版分离，test没有发起模型筛查。数据为本次编码助手编写的合成函数修复规格与固定literal判据，不是真实issue，也未经独立人工难度标定。目标难度标签按每族1易2中1难记录，实际难度保持null，后续以train/val诊断检验，不能当作已确认分布。

v2独立判据新增统一输入不变性检查、顶层返回类型与声明的ValueError判据。仅异常边界出错的任务也导出一个可执行可见复现；其余判据与参考解不进入工作区。原v1 exporter/verifier保持原样，新任务通过v2路径执行。

本地校验先发现并修正了转义fixture以及一条等价mutation；这些在模型筛查/冻结之前纠正，未删除任何模型失败。随后336项真实禁网容器门禁全部通过：96条bug拒绝、96条reference通过、96条固定错误解拒绝、48条适用的输入修改错误解拒绝。证据`expansion-v2/phase-a/task-gates-01/{manifest,sources,queue,summary}.json`，编程测试79项随后增加backend/rollout契约后共84项通过，strict mypy60文件通过。

开发对照入口`harness_comparison.py`：旧train16+旧pagination/deduplicate各4，共24个已公开开发case；每题original与json_newlines各一次，pair内seed一致、顺序交替。**不混入新test**。唯一变化是追加明确的JSON换行转义/避免triple quote指引；工具、Loop、采样、验证、步数/Token限制相同。新`rollout_v2.py`和`backend_v2.py`为实验层扩展，原已冻结模块不改。

运行前固定选择规则：候选成功数不下降，且格式拒绝/模型调用比例至少下降30%才采用；两者均无拒绝则保持original。不按最好案例或新test改规则。共享v2累计上限50、A上限8；已发生的校准0.32保守额加48条每条最多8次、每次最多0.02，完整最坏值正好8元，因此只读历史预留不释放也能准入本开发块。后续128条筛查/8条Smoke仍需依据账单和剩余额度准入，未据此取消原分母。

本次运行目录`expansion-v2/phase-a/harness-comparison-01`。启动前核对新任务门禁源码hash，冻结全部核心/实验源码和锁文件摘要；未知RPC将停止队列并留预留，不自动重复运行已提交行。

## 开发对照完成（2026-11-07）

48/48条开发轨迹已结束，进程正常exit0；原始候选hash、独立判据、Token旁路与队列逐条核对通过，全部源码hash吻合。运行时源码已另存`harness-comparison-01/source.tar.gz`，后续扩展不覆盖此版本。

| 开发组（非新test） | 独立成功 | 格式拒绝/模型调用 | 总Token | 工具平均次数 |
| --- | ---: | ---: | ---: | ---: |
| original | 14/24 | 11/165，6.67% | 215545 | 5.8333 |
| json_newlines | 16/24 | 11/169，6.51% | 225128 | 6.0417 |

候选成功多2条，但格式拒绝比例没有下降30%，因此**按预声明规则保留original**，不事后改为只看成功数或Token/成功挑方案。下一阶段选择保存在`phase-a/harness-selection.json`，绑定分析与新任务manifest hash；新test仍0次模型调用。此结果仅作开发选择，不能作为RL收益或新任务泛化证据。

本次对照334次请求，输入417659、输出23014 Token；无缓存连续估价0.72679547元，账本仍保守记6.68元。加计费校准0.32，A已占用7元保守额；v2累计commit=历史31.38+不确定预留2+新调用7=40.38元，pending=0。它不是实际扣款40.38。当前尚未将新128条train/validation筛查或8条训练Smoke认作已运行；仍需完成账单/精细计费核验后整块准入，不以重置预算文件绕过额度。

本回合原项目1072项检查通过（49.82秒），实验代码60文件strict mypy通过、84项全套检查通过。新增离线选择分析器及2项规则检查后，62文件strict mypy通过，82项纯/离线检查通过、4项integration本轮已先行通过而未在真实rollout期间重复运行。格式/lint通过；组件门禁336项与Coding性能数字分开报告。

下一执行项：确认已结束账期覆盖与费用估价规则，保留旧账本/闭合账本hash后进行可审计结转；再使用已冻结original对新train48/validation16各两次筛查，检查有效学习信号，之后8条训练Smoke与40槽Pilot。目标保持进行中。

## 估价结转与新任务筛查（2026-11-07）

只读刷新显示11月6日23–24时新增0.47元模型账单；此前20–23时3.40元未变，目前已入账合计3.87元、余额53.88（现金7.76、券46.12）。原始观察独立保存为`billing-observation-02.json`，当前小时/存储仍可能结算，不作为逐run最终账单。

新增`reprice.py`核对原core train/eval及本开发对照的全部原始sample与Trace/结果Token计数；只把可核对完整cohort的采样估价从逐类别整分改为逐类别向上取整至0.001元、不计缓存。**0.001是本实验更细的保守估价单位，不宣称是服务最低收费单位**。原非采样与S0保守额度10.06元完整保留，不按3.87元将全部消费清零；此外继续保留2元不确定额。

`reconciliation-01/audit.json`保存数学过程、源文件hash、父账本hash和账单观察hash；结转carryover=14.79元，叠加2元不确定额后commit=16.79，available=33.21。原`core-budget.json`31.38与v2旧账本40.38原样保留，禁止并行写入；本次是带证据的shadow估价变更，不是退款、重置消费或确认实际扣款。

新增`CalibratedBudget`控制器持有所有父账本锁，绑定结转、报价及观察证据，保留历史operation ID防重放；新固定共享账本`expansion-v2/calibrated-budget.json`。父证据变化、未知请求、磁盘写失败仍停止。新`PricedBackend`只改变费用量化，原模板/采样/Token旁路不改；当前4B价格刚通过`service-preflight-02.json`重新核验，仍是同一价格版本与单位。

新队列`screen_v2.py`：train48/validation16各两次，共128条，固定seed37洗牌及每条采样seed；新test仍0调用。使用已冻结original指引，判据为expansion_v2。A阶段总额8元继续生效，包含已运行开发对照/校准的重估消费；每run准入保留1.5元训练Smoke余量，并保留20元全局Pilot余量。触顶保留未运行行和完整分母，不偷偷删题。

运行目录`expansion-v2/phase-a/screening-01`，记录源码、判据、选择证据、配置与完整队列；先查pending再创建sampler，无自动补采样和未知RPC重试。筛查不是训练、不是新test结果；完整128条后才判断任务池可解性和学习信号。

### 筛查进行中与复核

本回合原项目1072项检查通过（50.52秒）、实验包94项检查通过（3.74秒）、70文件实验strict mypy通过；root格式/lint、175文件核心strict mypy、架构检查通过。新金融估价契约包含超时不重试/预留不释放、父证据变化拒绝、历史operation不重放、非采样旧费用保留、空/不完整cohort拒绝等检查。

`screening-01`实际进程已运行并返回前27条结果；剩余行保留not_run，尚未生成完成summary，不提前作学习收益判断。原始源码已核对并保存`screening-01/source.tar.gz`。本机工具执行会话为93450，最新一次poll明确仍live；继续时观察同一会话，不能因为等待超时或目录存在而重新启动。当前有正常在途RPC预留，并非已确认未知请求。

后续需继续全部128条筛查及完整审计；随后按任务可解性/数据质量门禁执行8条训练Smoke与40槽Pilot。未将预算估价调整或局部成功当作整个目标完成，也未使用含义仍不明确的重置卡。

## 完整筛查审计与训练 Smoke（2026-11-07）

128/128条完成，独立成功75条；train55/96、validation20/32，共1186473 Token。23/64个case的两次结果不同；整体成功率58.59%，位于预声明20%–80%诊断区间。原始Token/队列/候选hash/独立判据/源码已完整核对，unknown provider usage为0，新test仍0次模型调用。离线报告`phase-a/screening-analysis-01.json`绑定全部输入hash；这些是开发筛查，不能作为RL收益或最终泛化结果。

A采样影子估价4.200元，累计commit19.827（含2元不确定预留），不是服务实际扣款。基于完整train/val轨迹长度，对40槽训练+64评测估算：有效更新比例40%/80%/100%时分别12.23/18.43/21.53元；使用两条Base轨迹复制近似G4 padding，尚非真实训练费用。为容纳长度波动，将P阶段分配从20调整为**25元（含5.12元独立验证预留）**，累计仍≤50，不启用100/150档；只按资源实测修正分配，没有按模型收益改题或规则。若完整下一组和评测预留装不下，保留未运行分母并停止。

首次训练Smoke目录`phase-a/smoke-train-01`在4条采样后、本地`budget.reserve`处因历史basename operation ID冲突终止；**没有提交forward/optim请求**，pending=0，原journal保留，不自动重放。失败证据`failure.json`及完整源码archive已保存。修复采用完整实验目录hash作为训练operation命名空间，并把submitted journal转移到成功预留后、实际RPC之前；新增同basename不同目录与本地预留失败不提交两项回归检查。旧冻结原core训练模块、旧账本不改。

该工程块未作为成功Smoke验收；没有官方公开API可直接重连该未保存Train state，因此在`phase-a/smoke-train-v2-02`从新LoRA运行原2组×4工程任务。额外4条失败工程采样明确增加到执行账本，A原184条主体队列不删减，总工程Coding执行分母为188。其新增费用保留，A实际影子费用继续受8元约束；没有重置费用或用新目录隐藏已发生支出。后续Pilot仅在新Smoke完整2组且至少1次真实更新后准入。

### Smoke 通过，Pilot 已启动

`smoke-train-v2-02`正常exit0：2/2槽、8条rollout，奖励分别[1,1,0,0]和[1,1,1,0]，2次非零更新。采样78262 Token，新增保守估价1.509元；加首次已保留失败工程块后，全局commit21.551元（含2元不确定额）。训练State和初始/最终Sampler权重已持久保存，`audit.json`重建原始Token→Mask→Advantage→归一化Datum，2/2组hash相符、数值guard通过。源码完整归档且hash核对通过。它是工程训练验收，不是任务收益结论。

启动`expansion-v2/pilot-train-01`：从独立新LoRA开始，48个train用固定seed17洗牌的120槽顺序前40槽，每槽G4；保持lr1e-5/rank16/成功奖励/8步原Harness。P总分配25元，预留64条配对validation最坏5.12元，不拿开发筛查的plain Base替代本次初始LoRA Base。新test仍不调用；40槽checkpoint为预声明评测点，不按中途最好结果选模型。

新增`evaluate_v2.py`与`analyze_v2.py`：validation16×2×2策略=64，test32×2×2策略=128；同case/repeat配seed、策略顺序交替。只有显式绑定final checkpoint的决策才允许打开test。分析重建训练Datum及全队列独立验证，报告配对家族区间与原始失败分母。训练运行时不修改其捕获的源码；新分析模块单独记录版本。

### Pilot 前8槽与真实权重证据

Pilot已完成8/40槽、6次非零更新，含全失败和全成功各1组按规则跳过；并未补采样填充有效组。中途训练成功率仅作诊断，不作评测收益。前7槽格式拒绝19/178（10.67%）、provider失败0；继续监测15%停止门槛，数值guard持续通过。

只读下载新Smoke已保存的initial/final adapter（无新增训练/采样RPC），以safetensors逐张量计算实际LoRA差异L2=0.06624170582746909>0。证据`smoke-train-v2-02/weight-evidence/result.json`含两个archive hash和policy URI；确认实际权重变化，而不以空的optim metrics替代。该证据仍只支持工程训练成功，不能支持任务成功率提升。

本次代码检查：实验包110项检查通过（含4项真实Docker integration）、80文件strict mypy通过；主项目1072项回归通过（49.56秒）、175文件strict mypy和112模块架构检查通过。实验环境Ruff0.17.4通过，根环境Ruff0.16.8指出一处SDK类型import换行差异；运行期间保持冻结源码，格式统一留到该协议结束后处理，不将其报告成全部lint已通过。

只读UI账单刷新本次因Chrome无可用窗口/浏览器host unavailable未成功；最新已观察账单仍为前次3.87元快照，不能据此声称当前总费用。预算继续使用保守无缓存估价及2元不确定预留，不因无法刷新而归零。尚未使用重置卡，也没有充值或提高累计上限。

### 30槽 checkpoint 与预算内终点评测规则冻结

Pilot已完成30/40槽、120条训练轨迹、22次非零更新；格式拒绝62/773（8.02%），provider失败0。20/30槽Train state均已保存，不按中途结果选checkpoint。实际训练成功/失败仍仅用于组内Reward与诊断，没有用它宣称RL收益。

在训练进行中且新的配对validation/test均未开始时，冻结了[终点评测优先级调整](pilot-budget-amendment.md)：本轮最终checkpoint固定40槽，在完整训练/审计/64条validation完成后，**不论val是否提升**，仅依据完整性、数值与剩余资源准入128条新test。原60/120槽延长及成本Reward臂推迟，累计上限仍50。原分析器的`allow_extension`保留诊断，不能自动触发被推迟的阶段。

v1保留原0.08元/run余量，见`budget-priority-amendment-01.json`；随后在val/test开始前核对实际硬限额及逐请求计价，v2以数学上界收紧未来预留：连续最高0.04200640元，加8请求×2类别×0.001元取整余量，严格低于0.05800640元，因此0.06元/run、128条7.68元足够覆盖。证明`final-test-bound-01.json`绑定Model/Loop/Rollout/计费源码hash；`budget-priority-amendment-02.json`绑定证明及v1 hash。保留全局2元不确定额，没有释放旧消费或改任务Token上限。服务须遵守请求max_tokens和冻结报价；异常超量保留并停止，不能伪装已知零用量。

新增`evaluate_final_v2.py`保留原配对队列及Agent执行函数（AST检查相同），只增加证明核验并使用0.06元/run的未来财务预留；10000组确定性Token分配测试通过。validation仍使用原`evaluate_v2.py`的5.12元完整预留。新增`final_admission.py`在验证报告/最终权重/上界证明/共享账本一致且剩余≥7.68时签发决策；不足则新test完整分母保持未运行，不挑题、不上调限额。当前仍未调用新test。

## Pilot 训练完整完成，开始配对 validation

`pilot-train-01`正常exit0：40/40槽、160条训练、28次非零更新（70%），无停止原因。训练采样1326134 Token、999次模型请求；训练任务独立成功90/160仅作训练诊断，不能与Base筛查不配对地解释为学习收益。新增保守费用15.134元，累计commit36.685元（含2元不确定预留），stage25内保留完整validation。

`trajectory-audit.json`全量通过：160/160候选有独立验证记录，所有组的任务/顺序/seed/policy progression、原始Token元数据、Trace、Reward、Advantage、Mask与归一化Datum hash吻合；unknown provider failures=0，known local pre-RPC failure=0。按run_id保存判据绑定、候选与判据hash；训练State和原始证据均保留。

只读下载本次initial/final adapter，两者L2差异0.2775401521788983>0，见`pilot-train-01/weight-evidence/result.json`，含文件hash。导出期间与validation最初几条并行，属于只读artifact下载、没有额外训练/采样；本机/网络负载不受严格控制，端到端延迟只作探索性描述，不凭这轮小样本断言速度提升。

启动`pilot-eval-01`：validation16题×2次×2策略共64条，原始执行函数、同pair seed及交替顺序冻结，财务分配5.12元。Base为本次Pilot的初始零LoRA，RL为40槽最终LoRA；不得用plain Base筛查替换。当前只有局部轨迹，尚无完整val结论；新test仍0模型调用。后续先完整分析并确认剩余额度，再按amendment-02签发是否执行128条新test的资源决策，不以val方向择优开题。

## Validation 完整结果与新 test 启动

`pilot-eval-01`正常exit0，64/64条完成，独立候选/Trace/Token/策略绑定审计通过。Base初始零LoRA成功10/32、RL最终LoRA17/32；总Token分别298975和248937，Token/成功29897.5和14643.35，工具均值5.46875和4.875。成功率配对差值+21.875pp，4家族聚类bootstrap区间[0,+43.75]pp，不能称稳健增益；Base-only3对、RL-only10对。原`paired_interval`返回说明字符串沿用旧“四个test家族”模板，本轮此处实际是4个validation家族，以`families`字段与本段定义为准。分数不能与plain Base开发筛查直接比较，seed与sampler路径均不同；主对照只用本次匹配initial/final adapter。

验证推理保守费用1.404元，累计commit38.089、available11.911。`final-test-decision-01.json`按事先冻结amendment-02、完整训练轨迹审计及财务上界准入，`allow_final_test=true`、`allow_extension=false`、`validation_improvement_used_for_admission=false`。没有因val正向而更改规则或改选checkpoint。

`final-test-01`已启动128条新test：8家族×4变体×2重复×2策略。同pair seed、策略顺序交替，完整财务预留7.68元。新test未参加先前模型筛查/选Harness/选参数；打开后不再调参、增训练或额外选择checkpoint。其完整源码165文件及hash另存`full-sources.json`和`source.tar.gz`。尚未完成全部test，不提前下泛化结论。

导出配置核验：LoRA rank16、alpha32、dropout0、bias none；实际target modules为gate_proj/k_proj/q_proj/o_proj/v_proj/down_proj/in_proj_z/out_proj/up_proj，见`weight-evidence/adapter-config-evidence.json`，不能假定仅传统7个Linear模块或SDK允许调整全部PEFT字段。initial的248个B矩阵L2=0，final B的L2=0.26574357913601826；支持匹配Base确为零LoRA，完整adapter变化0.2775401521788983。云端基础权重revision/硬件型号仍未暴露，不宣称跨环境bitwise复现。

新增离线`secondary_statistics.py`对Token/成功、工具均值及端到端时间差做同family配对bootstrap；零成功统计为null，undefined bootstrap次数单独报告，不把失败Token删除或当零费用。`pilot-secondary-01.json`绑定输入与评分器hash；这些是探索性区间，不能消除单训练seed及少家族的不确定性。

## 新test完整结束与离线收尾

`final-test-01`正常exit0，128/128完成，新增保守费用3.074元，全局commit41.163元（含2元不确定预留），pending=0。结束时165个冻结源码hash均一致；`closure-budget-01.json`只读封存账本和时间，之后没有新的训练/采样请求。原始Trace、Token、候选与判据没有改写。

主结果Base28/64→RL34/64（43.75%→53.125%），family-cluster差值+9.375pp，95%区间[−10.9375,+26.5625]pp，8个test家族、64配对、10000次重采样。成功率尚无稳健提升证据。Token/成功下降14.24%，区间[−44.76%,+48.18%]跨0；总Token增加4.14%，每任务工具调用增加0.546875。格式拒绝65/423→35/430，差值−7.23pp、区间[−11.06,−3.99]pp，是探索性结构可靠性改善，不等同整体效率提升。

test私有候选验证覆盖127/128；Base另1条workspace allowlist拒绝仍计入完整任务分母。没有provider用量未知或model_call_failed。`query_pairs`4/8→0/8为明确退化家族。典型正向/反向案例以及工具/Token/时长、费用与后续30元增量建议均写入`docs/results/trainable-agent-expansion-2026-11-07.{md,json}`；源判据绑定和格式区间的派生脚本/报告保存在`final-tool-evidence-01.json`及同目录脚本中。

实验结束后仅整理`trainer_v2.py`的SDK类型import alias，使两版Ruff均接受，记录`post-experiment-style-01.json`，没有改RPC/训练逻辑；旧运行源码archive保留。用archive在临时目录离线回放val/test，完整JSON报告均与原报告逐字段相同。实验包131项检查通过（3.56秒），90文件strict mypy通过。主项目最终检查见`quality-gates-01.json`；测试数量不替代本报告分层验收与未测范围。

本轮范围为累计50元内的40槽成功奖励+完整独立test；60/120槽、更高预算、成本Reward和SFT未执行。后续先做账单对齐与任务/失败盲审；新的两Reward小对照必须新冻结配置与未用于调参的test。未使用重置卡，没有清空消费、充值或启用100/150档。

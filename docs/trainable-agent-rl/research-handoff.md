# RL 实验工作总结与继续启动手册

更新：2026-11-09。范围：11 月 6–9 日的训练、数据判据、交互反馈、学习曲线、GSPO 与故障恢复工作。
这是仓库内的研究交接文档；路径均相对仓库根目录，服务凭据、签名 URL 和远端任务标识不收录。

**下次从这里开始：先读本页“当前停点”和“附录 A”，再选择一个实验问题。不要按历史日志中的“下一步”直接运行旧队列。**

当前停点：

- 函数修复：GRPO 与 GSPO 各完成一轮 40 个 G4 组的训练和完整 Base 配对评测；有正向点估计，均未证实稳定收益。
- 多文件修复：v4 的 24 组学习曲线完成，但发现运行中/最终候选策略不一致及方法白名单误拒；离线修订分数不是新训练结果。
- 最值得先做的候选：按 `curve_pure_v2` 完成少量固定训练题的正控，先验证管线能否可靠拟合；**目前只有协议、队列和契约执行器，真实 provider 适配尚未完成**。
- 本次总结没有启动新训练、采样、账单核查或云端上传，也没有改变既有实验判据和分数。

## 1. 问题背景

研究问题是：在相同模型、提示、工具、执行预算和独立判据下，多轮代码修复轨迹上的强化学习是否提高成功率？
目标逐步从“训练闭环能否运行”转为“是否存在可学习的反馈信号、训练是否有效、效果能否重复”。

Mini-dsh 核心仍是 Harness/Runtime，不训练模型。所有 RL 能力在
[`experiments/trainable_agent/`](../../experiments/trainable_agent/README.md) 内，复用核心 Loop、权限、隔离和 Trace。
不能把平台功能、工程测试通过或参数变化本身写成 RL 效果。

这里有两条不同的数据主线：

| 主线 | 任务含义 | 研究用途与边界 |
| --- | --- | --- |
| 函数修复 `expansion_v2` | 24 种函数问题、每种 4 个缺陷变体，共 96 题；train48/validation16/test32，按问题家族隔离 | GRPO/GSPO 固定基准复测；虽有文件入口依赖，主体仍是函数修复，不能称复杂仓库修复 |
| 多文件修复 `minirepo` / `curve_*` | 跨模块缺陷；曲线重点是路由匹配、依赖分批、表连接三类任务 | 反馈利用与学习曲线；三个“家族”是任务类型，不是三个项目或三次独立实验 |

“路由匹配”涉及路径规范化、方法匹配、最长前缀；“依赖分批”涉及依赖关系和可执行批次；
“表连接”涉及连接模式、输出顺序及未匹配值。各版本具体规格以冻结 manifest 为准。

## 2. 方案决策

| 决策 | 实际做法 | 原因及取舍 |
| --- | --- | --- |
| 固定小模型与训练配置 | Qwen3.5-4B、LoRA r16、训练 seed17、G4、lr1e-5 | 先隔离数据/反馈/优化问题；各实验资源配置仍须分别读取，不能全局套用同一参数 |
| 可执行成功奖励 | 独立测试通过为 1，否则 0；参考解/私有判据不进入 Agent 工作区 | 不依靠模型自报完成；静态拒绝与真正执行功能测试的覆盖分别报告 |
| 保留原始训练输入 | 每次调用保存真实 prompt/completion Token 与采样 logprob；不重编码解析后的工具文本 | 保证训练数据能重建，仅本轮 assistant 动作参与直接损失 |
| 简化 GRPO | `A = reward - group_mean`；无组内 std 归一化、无 reference KL；native PPO clip | 全 0 或全 1 的 G4 没有相对奖励信号，跳过更新，不补采凑成功 |
| 先准入再扩大 | 判据 → 混合奖励/反馈修复 → 固定训练 → 配对验证 | 420 项判据通过也不能替代开发可学习性通过 |
| 固定终点与完整分母 | 不用最佳中间点替代终点；失败、预算终止和未运行机会均保留 | 防止结果驱动筛选；未运行不记成实测 0% |
| 版本与故障隔离 | 新版本、新目录、源码/输入 hash；未知提交不盲目重放 | 多花一些控制面工作，换取结果可审计及继续执行的明确边界 |

数值上的关键纠正：native PPO 后端实测为 `sample_mean`，需要补偿到 assistant-token mean；
PyTRIO 0.2.9 的 custom-loss CE 路径实测为 **sum**，GSPO 不能照搬前者再乘 Datum 数。
首轮 GSPO 数值探针曾因此失败，修正后 single/duplicate 更新比约为 1，旧失败保留。

本次 GSPO 为 `gspo_multiturn_wide_clip_v1`：完整轨迹 assistant Token 的几何平均 ratio、
轨迹等权、clip=.2/.2。不是论文默认窄裁剪的精确复现。与历史 GRPO 的长度权重不同，
所以“同规模”不等于已经完成仅改变裁剪粒度的 GRPO/GSPO 因果对照。

## 3. 已完成动作及实现入口

以下区分用户决策与 AI 辅助实现，不将对话自动化产出全部归为用户独立手写。

- 用户明确确定：RL 项目与 Harness 项目分开表达；先修判据和反馈、固定学习曲线，再扩大复核；保留负结果；按原 GRPO 规模验证 GSPO；停止额外用量统计。
- AI 辅助工作：依据这些指令实现/修订实验包、测试、调度与分析，执行真实实验并整理本地证据。
- 实际个人理解、手工代码占比和独立完成范围没有仅凭对话作进一步认定；若用于面试，需由用户确认。

| 工作 | 首要源码入口 | 已有能力 |
| --- | --- | --- |
| 函数任务与独立验证 | `tasks_v2.py`、`verification_v2.py`、`rollout_v2.py` | 原始 fixture、独立判据、候选范围限制与真实多轮轨迹 |
| 原始训练数据与 GRPO | `contracts.py`、`datum.py`、`trainer.py`、`trainer_v2.py`、`transaction.py` | 右移、mask、优势、后端归一化、更新事务与确认回执 |
| 多文件工具与判据 | `minirepo_tools.py`、`minirepo_verification.py`、`learning_rollout.py` | 读/搜/Patch/测试、候选策略、私有验证及轨迹绑定 |
| 数据与公开反馈版本 | `curve_cases*.py`、`curve_gates.py`、`development_cases.py`、`learning_feedback.py` | 参考/错误解/单缺陷探针、公开与私有判据隔离、反馈路径复核 |
| GRPO 曲线 | `curve_schedule.py`、`curve_execution.py`、`curve_sdk.py`、`learning_curve_run.py` | 0/8/16/24 检查点、固定队列、provider 接线及恢复 |
| 曲线离线审计 | `curve_analysis.py`、`curve_training_audit.py` | 逐条正确性、原始 Datum/优势/回执链重建 |
| GSPO 内核 | `gspo.py`、`gspo_trainer.py`、`gspo_numeric.py` | 整轨迹目标、自定义损失、梯度及数值门禁 |
| GSPO 效果实验 | `gspo_study*.py` | 40×G4、192 条配对评测、固定最终策略和完整评分 |
| 故障与权重证据 | `curve_recovery*.py`、`empty_optimizer.py`、`curve_weights.py`、`rpc_diagnostics.py` | 已终止初始化隔离、实际权重下载、空优化器等价性、脱敏错误胶囊 |
| GSPO 完整状态回滚 | `gspo_study_recovery.py` | 已终止反传任务隔离、非空 Adam 状态核对、训练后归档 READY 等待 |
| 正控（待接真实 provider） | `positive_control.py`、`positive_control_run.py` | 冻结基线、选题、18 组训练/评估状态机及 contract fixture；不是可直接运行的真实训练 CLI |

表中源码均位于 `experiments/trainable_agent/src/minidsh_rl/`。

## 4. 交付状态

**整体属于研究原型/技术验证，已有真实模型训练与评测；没有生产部署或真实用户业务收益证据。**

已完成：函数基准 GRPO、GSPO 同规模训练；多文件开发诊断与反馈修订；v4 单 seed 学习曲线；
数值与独立判据审计；必要的真实权重/优化器恢复核验；本地结果报告。

待完成：正控真实 provider 接线和实跑、修复候选策略后的新学习曲线、多训练 seed、
新家族迁移以及匹配长度归一化的算法对照。旧文档中的“下一步”是当时建议，不代表当前自动授权或已实现。

最新核对的本地状态：

| 队列 | 状态文件 | 完成/声明 | 确认训练组 / 实际更新 | 说明 |
| --- | --- | ---: | ---: | --- |
| v4 GRPO | `learning-curve-v4/run-03/curve-state.json` | 192/192 | 24 / 17 | `finished`，controller pending 为 null；原判据结果保留 |
| GSPO 同规模 | `gspo-efficacy-v1/run-03/state.json` | 352/352 | 40 / 25 | `finished`，controller pending 为 null；原失败目录保留 |
| 正控 v1 | 仅协议、源码与契约测试 | 无真实 provider 结果 | 未测 | 不能把脚本 fixture 的通过当成模型学习 |

以上路径前缀为 `benchmark-results/trainable_agent/`。controller pending 为 null 不等于历史资源账本没有未知请求。

## 5. 落地范围与版本边界

真实采样/LoRA 训练由 PyTRIO 托管服务提供，本机负责控制面、原生 arm64 Docker 工具执行与独立验证。
使用 Python≥3.12、uv、PyTRIO0.2.9；`lab` extra 包含 Torch2.9.1 与 SwanLab0.10.4。
核心默认安装不引入训练 SDK。具体模型与 tokenizer 身份、镜像 digest、源码、任务、预算都以各轮 plan/archive 为准。

| 版本 | 数据/判据与使用方式 | 不能混用之处 |
| --- | --- | --- |
| `expansion_v2` | 函数级96题；两个40组效果实验 | test 已打开；新的 GSPO 配对采样是固定基准复测，不是新盲测 |
| `curve_dataset_v1` | 30题、420项公开/私有检查 | 私有尾斜杠漏检；不得追认本版数据通过 |
| `curve_dataset_v2` | 修订私有单路由尾斜杠探针，420/420通过 | dev24/216后停，后续192条没有运行 |
| `curve_development_v3` | 同6道已打开dev，修订公开输入与反馈，私有判据沿用v2 | 是组合反馈profile，缺同期旧臂，不能把变化单独归因于反馈格式 |
| `curve_dataset_v4` | 排除6道已打开dev；12 train、12 validation，336项证据复用 | 原运行使用 `curve_pure_v1`；未评估新的最终测试集 |
| `curve_pure_v2` / 正控v1 | 运行中与最终验证统一策略，允许符合规格的额外字符串方法 | 修订尚无新真实学习曲线；正控评估仍是训练题的新采样 seed，不是未见题泛化 |

旧 learnability 实验中的 test 也已使用过。不能笼统说“所有 test 都封闭”或“所有验证都是新家族”。
SwanLab 仅是已明确上传轮次的数值派生视图；历史导入不算新实验，也不能推断最新 GSPO 已上传。

## 6. 效果证据与演进过程

### 6.1 实验台账（按研究推进顺序）

表中训练与评测规模按各轮原协议；**不要跨行相加成一个成功率，也不要把恢复目录当成独立训练 seed。**

| 阶段 | 类型/实际工作 | 结果 | 能支持的结论与证据 |
| --- | --- | --- | --- |
| 初始核心 GRPO，11-06 | 20组/80训练，8次更新；80条配对验证/测试 | test Base25/32→RL24/32 | 闭环实跑，但无正向收益；[报告](../results/trainable-agent-core-2026-11-06.md) |
| 扩展 GRPO，11-07 | 40组/160训练，28次更新；64验证+128测试 | test 28/64→34/64，+9.375pp；95%区间[-10.9375,+26.5625]pp | 正向点估计，未证实稳定收益；[报告](../results/trainable-agent-expansion-2026-11-07.md) |
| GSPO 迁移工程验证 | 修复custom-loss缩放；2组/8条Smoke，2次更新 | 实际权重变化、mask及single/duplicate数值探针通过 | 当时仅工程验证，无held-out效果；[报告](lab-migration-results.md) |
| H1 多文件交互诊断 | 8题、两种工作流×两档单次输出，共64条，无训练 | 四个臂均1/16；放宽输出后Patch提交更多，修复率未升 | 输出容量缓解不等于正确性收益；[报告](minirepo-results.md) |
| H2 批量读取 | 32条同期开发对照，无训练 | 单文件/批量读取均3/16 | 有反馈修复路径，无成功率提升；[报告](batch-read-results.md) |
| H3 四/八路径读取 | 32条开发对照，无训练 | 均4/16；八项组反馈修复覆盖2类任务 | 综合门槛仍失败；[报告](batch-limit-results.md) |
| 公开反馈覆盖审计 | 冻结错误候选探针，无模型训练 | 原公开测试漏掉8/8语义错误候选；独立草稿检出 | 定位反馈盲点，不证明模型能利用新反馈；[报告](feedback-coverage-results.md) |
| 新家族夹具/桥接/调度 | 224项夹具检查、原始轨迹绑定、80行队列及SDK契约 | 离线/脚本验收后才进入真实实验 | 不把契约测试当模型结果；[夹具](learning-fixtures-results.md)、[桥接](learning-bridge-results.md) |
| 交互可学习性 GRPO | 16准入+32训练+32测试=80；3次更新 | test Base7/16→RL4/16 | 未观察到收益；大量全零组，难度与信号不均；[报告](learnability-grpo-results.md) |
| 固定反馈示例 | 16条提示对照，无训练 | 原提示0/8→示例3/8；混合组0/2→2/2 | 提示可行性信号，不是RL收益；[报告](feedback-example-pilot.md) |
| 示例一步 GRPO | 1次更新+8条新配对评估 | 原判据Base/RL均0/4；影子判据双方均1/4 | 更新真实但未观察到收益；影子评分不改正式结果；[报告](example-grpo-step.md) |
| 学习曲线v1 | 420项数据检查 | 私有判据漏检4个尾斜杠单缺陷实例 | 数据门禁失败，后续216模型轨迹未运行；[报告](learning-curve-results.md) |
| 学习曲线v2 | 新判据420/420通过；24条真实dev | 成功7/24，混合2/6只覆盖1类，反馈修复只覆盖1类 | 准入失败；训练96/验证96未运行；[报告](learning-curve-v2.md) |
| 开发反馈v3 | 84检查通过；24条真实dev，无训练 | 成功8/24，混合4/6覆盖2类，反馈修复3条覆盖2类 | 原开发门槛通过，不证明RL有效；[报告](development-feedback-v3.md) |
| 多文件GRPO曲线v4 | 24组/96训练+96验证；17次更新 | 固定检查点7→8→6→8，各/24 | 探索门槛通过；+4.17pp，95%区间[-37.5,+25]pp；[完整结果](learning-curve-v4-recovery.md) |
| 候选策略审计与正控准备 | 离线复评分、白名单/运行中策略修订、正控队列和fixture | v4原分数不改；诊断曲线8→8→8→10 | 离线诊断，不是新权重/新采样收益；[协议与诊断](positive-control-v1.md) |
| GSPO同规模效果，11-09 | 40组/160训练、25次更新；64验证+128测试 | test 28/64→33/64，+7.8125pp | 正向点估计，95%区间[-1.5625,+17.1875]pp；[完整报告](../results/gspo-efficacy-v1-2026-11-09.md) |

### 6.2 最新两个效果实验

| 实验 | 验证 | 测试（主要终点） | 边界 |
| --- | --- | --- | --- |
| 历史扩展GRPO | Base10/32→GRPO17/32 | 28/64→34/64（43.75%→53.125%） | 单训练seed，区间含0 |
| 最新GSPO | Base10/32→GSPO18/32 | 28/64→33/64（43.75%→51.5625%） | 单训练seed，固定既有基准复测，区间含0 |

两个 Base 成功数恰好相同，不表示复用了同一批原始评测。GSPO 的 Base/GSPO 均重新采样。
34/64 与33/64相差1次成功，不能据此给算法排优劣；当前两轮不构成长度归一化匹配的单变量对照。

最新GSPO测试中，GSPO独有成功12对、Base独有成功7对，净多5/64；
训练25/40组有效更新，15个全同奖励组跳过。全部40组Datum、25组loss/mask、352条轨迹评分复核通过。
最终真实adapter相对初始L2差约0.265292；它证明更新实际发生，不证明成功率稳定提升。
根项目1072项、实验包358项测试通过，不能用这些数量代替研究效果验收。

### 6.3 多文件曲线的已知判据/策略问题

v4原官方曲线为7/24→8/24→6/24→8/24。后来发现：

- 最终验证采用 `curve_pure_v1`，运行中 `run_tests` 却仍用legacy策略，导致符合规格的 `issubset` 等调用被误拒。
- v1还会误拒 `.rstrip`；新 `curve_pure_v2` 增加 `rstrip/lstrip/replace`，运行中与最终检查统一。
- 离线重放192条最终候选：策略拒绝20→8；诊断验证曲线8/24→8/24→8/24→10/24。
- **不覆盖原分数，不称重新实测。** 运行时反馈本身曾被影响，因此事后复评分无法还原“当时若反馈正确会怎样”。

这使“先验证修复后的管线能拟合少量任务”比直接解释弱家族退化或盲目扩大训练更有价值。

### 6.4 发生过的恢复及禁止泛化的例外

| 故障 | 实际处置 | 下次能否照搬 |
| --- | --- | --- |
| v4 run-01/02初始化恢复失败，第二次HTTP404 | 证明原任务已终止且未采样/更新；相同seed fresh LoRA，实际参数差0、空优化器配置一致后完成run-03 | 仅适用于未训练的空状态；不能替代已训练Adam恢复；[ADR0053](../adr/0053-initialization-recovery.md)、[0054](../adr/0054-equivalent-empty-optimizer-start.md) |
| GSPO第31组反传查询EventPollingError | 已完成124条采样、30组/19次更新；旧Actor owned/ENDED，尚未提交该组optim；新Actor恢复完整检查点，实际权重差0、非空Adam逐项相等后重建独立事务 | 必须符合终止、阶段、回执和实际状态条件；不是允许重放未知optim；[ADR0057](../adr/0057-gspo-terminal-backward-rollback.md) |
| 训练后约187MiB归档尚未READY | 保留两个只读等待停点，最终修订为60次×5秒有界查询；不重采原124条轨迹 | 这是归档就绪问题，不是增加训练剂量或放宽正确性；最后完成在GSPO run-03 |

函数基准两个40组实验、v4曲线以及恢复目录，均不能据目录数量算作多个独立训练seed。

## 7. 个人边界与可用结论

可以确认：已完成真实轨迹采样、奖励设计/判据检查、简化GRPO与指定GSPO变体训练、配对评测、数值和失败诊断。
本项目复用托管4B模型和LoRA能力，不是从头训练基础模型；没有证据表明底层训练引擎由本项目实现。

不应升级为：生产落地、SWE-bench成绩、复杂真实仓库泛化、多seed稳定提升、完整策略熵已测、GSPO优于GRPO，
或“放宽候选策略已在新训练中验证有效”。同家族实例也不自动等于语义独立或无预训练污染。

用户负责的研究目标/优先级与AI辅助编程和自动化执行需分别说明。本文不生成新的简历包装主张。

## 8. 待补证据与下一阶段建议

**推荐先完成正控真实接线，再决定重跑多文件曲线；这只是建议，本次没有启动执行。**

1. 核对并冻结运行中/最终验证均为 `curve_pure_v2`。用回归用例覆盖 `issubset`、`rstrip` 等合法用法，仍拒绝I/O等越界候选。
2. 为 `PositiveRuntime` 接入真实 provider：复用已验证训练内核、原始Token轨迹、初始权重审计、唯一资源租约及完整更新回执。
   当前 `positive_control_run` 是注入式控制器，不能因为文件存在就假定有CLI或可直接付费运行。
3. 核对正控基线口径：协议窗口每题1–4/8；冻结候选与回退顺序。生成器先排9候选×8seed=72条，
   选定3题的24条同时作为checkpoint0；不要误认为只有24条基线成本，也不要把后备或替换机会漏出声明分母。
4. 固定3题×6轮=18个G4组/72条训练；0/6/12/18检查点，每题8seed、每点24条，保持固定终点。
5. 原正控成功门槛：最终≥20/24、相对基线至少多8条、每题≥5/8；失败门槛：最终≤16/24且实际更新≥8，
   并排除提前安全/资源/零信号停止；其他情况为不确定。实现接线前核对协议和 `final_report` 在提前停止时的一致性。
6. 通过后才评估另外9道同家族训练题；这是迁移复核，不把正控过拟合分数当泛化分数。
7. 正控失败则先查优化、探索、奖励/优势和逐任务漂移；信号不足判不确定，不直接宣布RL无效或换算法。

后续独立事项：修复后多文件新曲线、约3个独立训练seed、同家族新实例/新家族分别评估、匹配长度权重的算法消融。
每次只推进其中一个明确问题；新实验须另立冻结协议、全新目录、明确主终点和停止条件。

尚缺：新策略真实训练效果、正控真实结果、多训练seed重复、新盲测/真实仓库迁移、严格匹配的GRPO/GSPO对照。
完整策略熵未测；不能用样本surprisal代理直接补填熵曲线。最新GSPO云端可视化是否已发布也不作推断。

## 9. 后续复盘与面试可追问的问题

- 为什么混合奖励要按同题、同snapshot的G4逐组计算，而不能按家族平均成功率推断？
- 如何证明模型使用了失败反馈，而不是最终恰好修好？对应partial snapshot、后续真实采样和新Patch在哪里？
- native PPO与custom-loss CE为什么需要不同缩放？single/duplicate探针实际测到了什么？
- 候选策略误拒会怎样同时污染在线反馈与离线评分？为什么离线复评分不能补认新训练收益？
- 初始空优化器等价重建与训练后非空Adam恢复，有哪些必须分别验证的条件？
- GSPO净多5/64与95%区间含0分别说明什么？为什么不能按34/64对33/64评算法优劣？
- 哪些设计/代码/诊断由用户独立完成，哪些由AI协助？能否从一个原始run解释完整证据链？

## 附录 A：5–10分钟回顾与安全启动

### A1. 推荐阅读顺序

1. 本页当前停点、版本表、6.3与第8节。
2. [正控v1](positive-control-v1.md)：当前候选下一实验和未实现边界。
3. [v4原始完整结果](learning-curve-v4-recovery.md)：原曲线及家族差异。
4. [GSPO完整结果](../results/gspo-efficacy-v1-2026-11-09.md)：最新函数基准效果。
5. 需要追因时再读各行原报告与ADR，不按旧execution-status顶部反复出现的“最新”推断时序。

### A2. 环境与本地验证（不启动模型实验）

从仓库根目录执行；依赖环境需要初始化时才运行第一条，保持锁文件约束：

```sh
uv sync --project experiments/trainable_agent --python 3.12 --extra lab --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy --cache-dir .mypy_cache/root src tests
MYPYPATH=src uv run --project experiments/trainable_agent --extra lab mypy \
  --cache-dir experiments/trainable_agent/.mypy_cache \
  experiments/trainable_agent/src experiments/trainable_agent/tests
uv run pytest
uv run --project experiments/trainable_agent --extra lab pytest experiments/trainable_agent/tests -q
```

两套mypy使用不同cache，避免同名模块缓存干扰。离线测试不应依赖真实模型服务。
读取凭据用既有SDK机制；不要将key、环境变量全文、签名下载URL写入文档/Trace/终端输出。

### A3. 无新模型调用的复核命令

先运行源码/队列检查；当前源码与旧冻结版本不一致时应停止并核对archive，不能刷新旧hash绕过：

```sh
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_study_run inspect \
  --directory benchmark-results/trainable_agent/gspo-efficacy-v1/run-03
```

以下示例将输出写到全新临时目录，保留正式结果文件：

```sh
RL_REVIEW_DIR=$(mktemp -d /tmp/minidsh-rl-review.XXXXXX)
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_study_analysis \
  --directory benchmark-results/trainable_agent/gspo-efficacy-v1/run-03 \
  --output "$RL_REVIEW_DIR/gspo-audit.json"
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.curve_training_audit \
  --directory benchmark-results/trainable_agent/learning-curve-v4/run-03 \
  --output "$RL_REVIEW_DIR/v4-training-audit.json"
```

GSPO正式JSON/Markdown生成器为
[`gspo_efficacy_report.py`](../../experiments/trainable_agent/scripts/gspo_efficacy_report.py)：只做离线分析，
但会写回正式派生报告，**不是默认只读检查命令**。未来代码变化时应保留旧scorer和报告hash，
不要把重算结果描述为一次新的真实模型实验。

### A4. 准备下一次真实运行时的顺序

- 先确定要回答正控、同家族迁移、新家族迁移还是算法比较中的哪一个问题。
- 写清dataset/public/private/candidate-policy版本、模型与初始状态、seed、G、奖励、剂量、检查点、主要终点和停止条件。
- 预先列出全部机会与分母，包括基线候选、回退、训练及评估；判据通过后再确认混合奖励/反馈证据。
- 使用新目录，保留旧失败；保存源码、锁文件、tokenizer、模型/LoRA、输入hash和独立判据绑定。
- 让运行器做必要资源准入，不额外开启账单UI、用量导出或反复费用核对。唯一活动epoch仍指向
  `benchmark-results/trainable_agent/learning-curve-v2/run-01/finance/`，不是让新实验新建账本清零。
- 两个v4初始化旧请求及一个GSPO反传旧请求的预留/隔离证据继续保留；其他未知请求仍阻断。
  是否能恢复取决于事务阶段和证据，不只看 `state.phase`。
- 真正启动后不要边训练边改被冻结的源码/锁文件；不要重用已完成run目录或机械地加 `--resume`。

这里故意不给一键付费启动命令：正控真实适配尚缺，不能把旧曲线或故障恢复入口冒充正控启动器。
本次仅整理交接材料；继续实验时按既有授权与当前明确范围推进，不因本文额外增加审批流程。

## 附录 B：原始证据定位

根前缀：`benchmark-results/trainable_agent/`。原始数据通常被Git忽略，不能只备份Markdown或HEAD。

| 内容 | 本地定位 | 继续时先看 |
| --- | --- | --- |
| 历史扩展GRPO | `expansion-v2/pilot-train-01/`；其余评测子目录见原报告 | 40组队列、weight-evidence、原始tokens/Trace及独立结果 |
| GSPO早期数值证据 | `lab-migration/gspo-smoke-02/` | `source.json`、numeric结果；仅在内核hash一致时复用 |
| v1/v2判据审计 | `learning-curve-v1/audit-01/`、`learning-curve-v2/audit-01/` | 原420项判据，v1漏检记录不得覆盖 |
| v2开发停点 | `learning-curve-v2/run-01/` | plan、readiness、24条轨迹；后续192条未运行 |
| v3开发准入 | `development-feedback-v3/run-01/` | 冻结公开/私有版本、feedback proofs和24条结果 |
| v4曲线及初始化失败 | `learning-curve-v4/run-01/`、`run-02/`、`run-03/` | 前两个无采样/更新；run-03是192条完整结果 |
| 最新GSPO完整与故障 | `gspo-efficacy-v1/run-01/`、`run-02/`、`run-03/` | run-01原反传故障；run-02只读等待停点；run-03完整结果 |
| 最新GSPO恢复与最终权重 | `gspo-efficacy-v1/run-03/rollback-sdk/`、`final-audit/` | 参考/恢复归档与逐项Adam一致性、最终参数差 |
| 最新GSPO审计/复现 | `gspo-efficacy-v1/report-reproducibility.json`及各audit JSON | 两次生成hash一致；不是两轮训练 |

每轮最小证据链：`plan/config → source archive/hash → initial policy → raw tokens/trace → candidate → independent verdict → group/update journal → receipt/checkpoint → derived report`。
查看 `state.json` / `curve-state.json`、`stop*.json`、`rollback-error.json`及started/ended记录定位停点。
只看到SwanLab FINISHED或Loop finished都不足以判任务正确、整轮完成或研究门槛通过。

当前工作树含多轮未提交改动及新增源码/报告；不能 `git reset --hard`、清理未跟踪文件或只按HEAD理解当前实现。
若需快照，分别保存源码/锁文件与本地实验原始产物；凭据、原始模型缓存、私有验证器和权重不要推入公开仓库。

## 附录 C：升维迁移（可选复盘）

原始事实：同规模GSPO训练40组，25次更新，测试28/64→33/64。

连接机制与影响的表达：以独立判据和原始轨迹重建确认更新有效、恢复前后完整状态一致，
在固定配对基准上观察到+7.81pp，但保留区间跨零与单seed边界，从而为下一次正控和重复实验提供可复核起点。

本次升维动作是把“跑过训练”拆成数据可靠性、学习信号、真实更新、任务效果与证据边界。
可选自测：用自己的话解释为什么“权重变了”和“验证+25pp”仍不足以证明稳定泛化收益。

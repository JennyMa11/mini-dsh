# Mini-dsh ¥50 RL 核心实验

> **继续 RL 实验先读：[近期工作总结与启动手册](../../docs/trainable-agent-rl/research-handoff.md)（2026-11-09）。** 汇总两条实验主线、最新停点、候选策略误拒、正控待接线与离线复核入口；以下历史“最新”段落按各自时点理解。

最新 [v4 完整学习曲线](../../docs/trainable-agent-rl/learning-curve-v4-recovery.md)：
96 条训练＋96 条验证已完成，24 个槽产生 17 次真实更新；成功数 7→8→6→8（各 24 条）。
最终 +4.17 个百分点，探索门槛通过但稳定收益未证实；三家族结果分化。初始化恢复失败
保留，run-03 经参数差0和空优化器配置一致验证后完成。本轮不开展额外用量核对。
以下为历史阶段记录。

最新 [学习曲线 v4](../../docs/trainable-agent-rl/learning-curve-v4.md)：排除已打开 dev，
336 项数据检查通过，固定 96 train + 96 validation；真实运行在初始 checkpoint
保存时失败，0 新采样/更新，学习效果未测。累计保守占用 52.264 元，含一项
0.100 元未知请求预留，禁止重放。唯一活动财务账本仍为
`benchmark-results/trainable_agent/learning-curve-v2/run-01/finance/budget.json`。
后文为各历史时点；最新入口为 `python -m minidsh_rl.learning_curve_run`。

最新[开发反馈v3](../../docs/trainable-agent-rl/development-feedback-v3.md)：仅修订6道已打开
开发题的公开反馈，84检查通过；24条真实开发成功8条，混合4组、反馈覆盖2算法组，
原准入门槛通过。无训练或学习收益结论；复用原零LoRA与唯一活动账本，新增1.515元
保守占用，累计52.104元，无pending。旧v2结果不变。

最新阶段为[单seed学习曲线v2](../../docs/trainable-agent-rl/learning-curve-v2.md)：
420项判据通过，真实开发24条成功7条，但混合组/反馈家族门槛未过，停止训练。
训练与验证192条未运行。累计150/新增100元预算已显式迁移，唯一活动账本为
`benchmark-results/trainable_agent/learning-curve-v2/run-01/finance/budget.json`；
旧50元入口已封存，以下旧余额与状态仅为各历史阶段记录。

当前阶段见[固定执行入口](../../docs/trainable-agent-rl/learning-execution-results.md)：
已实现离线prepare/inspect与受完整资金/配置/组件证据控制的显式run；历史数值仅复用
可核对旧组件证据，新初始LoRA仍须真实检查。80条provider完成数为0，完整需20.302元、
可用7.333元，未准入或实际run。历史结果及源码归档按各自版本保留。

新增可选 `lab` extra：多轮GSPO工程实验与SwanLab，见[迁移说明](../../docs/trainable-agent-rl/lab-migration.md)。核心训练结果与新Smoke分别标记；历史数据可回填到私有看板，不作为新评测。

独立实验包，Python 3.12、PyTRIO 0.2.9；不进入生产Runtime依赖。已实现原Loop多轮rollout、原始Token/logprob旁路、独立判据、GRPO/LoRA控制器、持久预算及训练恢复门禁。20槽主训练及80条固定Base/RL配对评测已完成；测试Base 25/32、RL 24/32，未观察到成功率收益。保守累计31.38元，详见[结果报告](../../docs/results/trainable-agent-core-2026-11-06.md)。不能将工程链路通过解读为RL有效。

安装与离线验证（在仓库根目录）：

```sh
uv sync --project experiments/trainable_agent --python 3.12 --locked
uv run --project experiments/trainable_agent --extra lab pytest experiments/trainable_agent/tests -q
MYPYPATH=src uv run --project experiments/trainable_agent --extra lab mypy experiments/trainable_agent/src experiments/trainable_agent/tests
uv run ruff check experiments/trainable_agent
uv run ruff format --check experiments/trainable_agent
```

只读服务预检，使用PyTRIO既有私有凭据，不输出Key，不创建trainer/sampler：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.preflight \
  --output benchmark-results/trainable_agent/preflight/service.json
```

复核所有候选任务的bug与reference（禁网、原生arm64独立容器，输出目录必须不存在）：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.gates \
  --output benchmark-results/trainable_agent/task-gates-recheck \
  --image python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
```

`tasks.py`包含控制面私有参考解及评分数据，禁止复制实验包/仓库到agent工作区。只导出公开4文件fixture，只有implementation.py可修改。任务已补充异常/不变性规则与纯函数限制；研究集按family固定16 train/8 validation/16 test，工程夹具4题单列。任务集仅为小函数修复，不能代表SWE-bench级仓库修复。

`BudgetStore`持久化并接入所有采样/训练准入，未知RPC保留费用且不自动重放；模型错误不会触发隐藏重采样。训练使用PyTRIO实际测得的sample_mean reduction补偿、只对当前assistant生成Token计算loss；观察和历史Token为零权重。零Advantage组跳过更新。

运行训练/评测会发生付费调用；已完成目录禁止重启。历史核心账本为 `benchmark-results/trainable_agent/core-budget.json`，limit=50，不可重置；当前活动账本见下文。最终训练state、sampler路径及恢复检查在本地artifact目录，Token/Trace及权重archive不入Git。离线分析无需模型调用，命令与结论见最终报告。

详见[轻量协议](../../docs/trainable-agent-rl/lightweight-experiment.md)、[实施进度](../../docs/trainable-agent-rl/execution-status.md)。

小仓库开发诊断：新增受控读/搜/Patch/测试工具与8个跨模块任务，完整运行64条并按门禁停止；尚未进入该任务集的RL。见[协议](../../docs/trainable-agent-rl/minirepo-plan.md)及[结果](../../docs/trainable-agent-rl/minirepo-results.md)。

后续仅离线分析：[失败分类与工具冷启动候选协议](../../docs/trainable-agent-rl/tool-cold-start-plan.md)。
分类脚本`minirepo_failure_review.py`只读已有证据；不把该设计文档当作新付费运行授权。

当前新目标：[交互可学习性执行记录](../../docs/trainable-agent-rl/interaction-learnability-execution.md)。
已接入工具示例及同期配对入口，确定性控制探针不算模型rollout；资金不足时不连接服务。
当前唯一活动财务账本为`benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json`，
旧`core-budget.json`仅为冻结历史账本，不能用来重新启动当前实验或释放历史消费。

工具示例同期32条已结束：[结果](../../docs/trainable-agent-rl/interaction-learnability-results.md)。
原Prompt3/16、示例4/16，反馈后修复门禁未过；当前优先减少读取回合，尚未开始新算法比较。

当前离线阶段：[H2可选批量读取协议](../../docs/trainable-agent-rl/batch-read-plan.md)。
实验工具增加显式`read_variant`，默认仍为single_read；批量模式1–4路径共享5000字符。
脚本控制只验证信息等价与反馈机会，不是模型收益；累计50元的完整队列资金不足时拒绝。
离线证据、复核入口及未完成项见[H2执行记录](../../docs/trainable-agent-rl/batch-read-execution.md)。
H2完整32条已结束：[结果](../../docs/trainable-agent-rl/batch-read-results.md)。两组均3/16，
反馈修复只覆盖1家族，未进入新训练；保守累计44.013元、剩余5.987元、无pending。

当前唯一阶段为[H3路径上限协议](../../docs/trainable-agent-rl/batch-limit-plan.md)：显式
batch_read_eight保留原5000字符、路径与隔离边界，默认四项模式不变。先通过离线门禁，
真实32条须完整费用准入，不用旧H2控制分数替代同期对照，不据参数拒绝减少宣称模型收益。

H3已结束：[结果](../../docs/trainable-agent-rl/batch-limit-results.md)。四项/八项均4/16，
八项组出现两个家族的反馈修复，但综合门槛未过；累计保守46.135元、剩余3.865元，
无pending或新训练。先离线收敛配对收益/退化及完整后续预算，不自动启动第四轮工具校准。

离线[反馈覆盖审计](../../docs/trainable-agent-rl/feedback-coverage-results.md)完成：原公开检查
捕获16/16原始缺陷，却漏检8个语义实例（4种唯一探针）。显式新ID的feedback_v2开发草稿
补齐这些定向反馈并通过同探针审计；旧任务及私有判据不变，无新模型/训练调用，仍未进入RL。

当前离线阶段：[新家族交互学习准入草案](../../docs/trainable-agent-rl/interaction-learning-readiness.md)。
[三小时费用覆盖审计](../../docs/trainable-agent-rl/feedback-hour-reconciliation.md)释放3.468元
过度预留，当前保守占用42.667元、可用7.333元、无pending，累计仍50元。
完整80条readiness/训练/评测拟预留19.702元，尚未准入；新任务与MiniTask训练桥接
尚未实现，不因草案或费用释放启动模型调用，也未完成交互学习收益或算法比较。

[新家族离线夹具](../../docs/trainable-agent-rl/learning-fixtures-results.md)已实现：16题/112探针，
224次真实容器检查完成，已声明错误均被两套判据检出；合并router/media后，v2数据
为3个train算法组和4个test组。只修订metadata并复用逐hash相同的检查，不重复实测。
`learning_gates`无模型或训练入口；typed训练桥接及新模型可学习性仍待验证，费用不变。

[typed轨迹桥接](../../docs/trainable-agent-rl/learning-bridge-results.md)已完成离线契约及四项
真实容器脚本控制；独立schema2绑定新版本，保留原始Token与失败，不改旧result/Trace。
脚本不能进入provider训练，已知预算超量保留消耗并阻止更新。仍无新provider采样/
训练，当前缺口为新任务完整调度、真实身份/数值和恢复准入，累计费用不变。

最新[单 seed 学习曲线阶段](../../docs/trainable-agent-rl/learning-curve-results.md)：
新增30题与显式curve_pure_v1判据，420次容器检查完整执行；私有判据漏检4个
尾斜杠缺陷实例，按门禁停止，未进入216条模型队列，未迁移预算或训练。
`python -m minidsh_rl.curve_gates --summarize ... --output ...`可离线复核派生报告。


2026-11-09：用户授权按历史 GRPO 规模进行 Base / GSPO 效果复测；160训练轨迹、
64验证、128测试，当前真实执行中。协议：[冻结协议](../../docs/trainable-agent-rl/gspo-efficacy-v1.md)。
旧test已使用，不称新盲测；不以历史GRPO成绩直接作算法优劣结论。


2026-11-09 Base/GSPO同规模实验已完成：[完整结果](../../docs/results/gspo-efficacy-v1-2026-11-09.md)。
352/352真实轨迹、40个G4训练组、25次有效更新；验证10/32→18/32，
测试28/64→33/64（+7.8125pp；95%家族聚类区间[-1.5625,+17.1875]pp）。
单训练seed、既有测试集复测，不能宣称稳定收益或优于GRPO。反传查询失败后按ADR0057
从实际权重与完整Adam状态全等的检查点恢复，原失败保留、124条轨迹未重采；
358项实验回归及前后容器隔离检查通过。不自动扩大新seed或算法对照。

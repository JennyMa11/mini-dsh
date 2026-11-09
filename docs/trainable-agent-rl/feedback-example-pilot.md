# 反馈示例学习信号诊断：协议与结果

日期：2026-11-08。用户明确要求运行一轮实验并记录对应数据。本轮在已有 50 元累计
上限内执行，阶段上限 **1.50 元**：16 条采样上界 1.44 元，已存在 sampler 的初始化/
元数据保守额度 0.05 元，共预留 1.49 元。运行前可用 2.765 元，无需充值或重置卡。
这是完整的新开发诊断，不删减或替代之前 80 条实验及其 test 分母。

## 问题与变量

检验已有的独立玩具工具反馈示例，是否改善多轮修复行为、让 G4 奖励出现差异。
采用现有 `minirepo_prompts.py` 的 `original` 与 `tool_example`，仅改变 system
instruction 的示例附录；不增加工具、修改 Agent Loop、改变 Reward 或更新模型。
示例不含本轮任务题解或独立验证输入。示例增加的上下文 Token 也是处理的一部分，
不宣称能单独区分示例语义与上下文长度影响。

任务固定为 `learnability_v1_path_router_0` 和 `learnability_v1_dependency_batches_0`，
来自原 train、两个不同潜在家族。选择依据是此前训练组均全失败，属于事后开发集
选择，不能作为新的独立留出集。每题每臂 4 次，合计 16 条，两臂同配对 seed：
`826000 + task_index*1000 + repeat*20`，任务内 repeat=0..3，逐对交替运行臂顺序。
不根据途中结果换题、补采样或调整参数。

两臂均使用此前实际核验的初始零 LoRA sampler、Qwen3.5-4B、temperature=1、
top_p=1、Full History、batch_read_eight；12 步/12 工具、36k 累计 Token、3072 总
生成、6144 单请求总长、1024 单次生成、180 秒 Loop。每条独立禁网容器，1 CPU、
1 GB 内存；结束后在新容器执行相同独立判据，候选静态拒绝计失败，保持完整分母。

## 指标、验收与解释

主指标：独立成功数 / 每臂固定 8 条，8 对的 discordance 与成功率差。另报告每臂
两道任务的 G4 Reward/Advantage、混合奖励组数、反馈后的新 patch、工具调用、
Token、错误、重复失败、端到端时间及全部失败消费。仅 2 个任务簇，不作稳健统计
或泛化主张；玩具示例有效也不等于 RL 已产生收益，不放行算法对照。

完整验收要求：16/16、有唯一 run_id、固定 case/seed/arm/policy 绑定、每次原始
Token/logprob/Observation/独立判据、财务 operation 及 UTC 起止可追溯，源码保持
冻结，未知 RPC/pending 为零，两个费用上限均满足。模型自报或 finished 不代替正确性。

报价、源码、归属、tokenizer 变化，未知请求或剩余完整队列资金不足时立即停止，
保留完整声明分母与消费。未知请求不自动重试或释放预留，不以失败为理由追加运行。

## 数据与复现入口

本地：`benchmark-results/trainable_agent/feedback-example-pilot-v1/`。
`run_pilot.py prepare` 冻结协议、队列、逐文件来源 hash 和源码 archive；`run` 在
完整资金准入、实时只读报价及既有 checkpoint 归属核验后才调用 sampler。
每次调用保留原始 Token、采样 start/return、Trace、候选、patch snapshots、独立
验证结果、逐调用财务 ID/估价/时间和 artifact manifest。原数据留本地，完成后仅
将允许的数值指标与封闭配置上传至私有 SwanLab 项目并回读。

运行前复用并执行记录、计价 backend、轨迹绑定和校准账本的现有契约测试；不修改
生产或实验包代码。结果将在本文件追加，运行前协议另保存在本地不可覆盖的
`run-01/protocol.json` 中，不能用结果改写预声明问题或队列。

## 实际结果：16/16 完成

执行时间：2026-11-08 12:22:32–12:29:53（Asia/Shanghai），约 7 分 21 秒，包含
初始化、采样、容器准备和独立验证。进程正常 exit 0，160 次真实采样，0 次训练更新。
204 个冻结源码/依赖文件保持一致，没有换题、补采样或改参数。

| 指标 | 原提示 | 工具反馈示例 |
| --- | ---: | ---: |
| 独立成功 / 声明数 | 0/8 | 3/8 |
| 成功率 | 0% | 37.5% |
| G4 混合奖励组 / 组数 | 0/2 | 2/2 |
| 公开失败→后续新 patch→最终独立通过 | 0/8 | 3/8 |
| 实际观察到公开失败的运行数 | 8 | 7 |
| 上述路径 / 已触发公开失败机会 | 0/8 | 3/7 |
| Token 总量，包含失败 | 156,661 | 250,281 |
| 全部 Token / 成功数 | 未定义，无成功 | 83,427 |
| 平均工具调用 | 7.375 | 11.125 |
| patch 总数 | 2 | 9 |
| 端到端 p50 / p95，n=8，nearest_rank | 26.67 / 56.56 秒 | 21.13 / 41.79 秒 |
| 采样保守估价 | 0.345 元 | 0.527 元 |

两组均有 8/8 独立判据，功能测试也实际执行 8/8，无候选静态拒绝。配对示例独有
成功 3 对、原提示独有成功 0 对，其余 5 对均失败；描述性差值 +37.5 个百分点。
仅 2 个经过事后选择的任务簇，本轮不报告容易产生误导的微小簇 bootstrap 显著性，
不据此推断新任务或 RL 的稳定收益。

### 组内 Reward 与潜在训练信号

下表按预声明 repeat=0..3 排序；Advantage 仅离线计算，**没有提交训练**。

| 任务 | 原提示 Reward | 示例 Reward | 示例 Advantage |
| --- | --- | --- | --- |
| path_router_0 | [0,0,0,0] | [1,0,0,1] | [0.5,−0.5,−0.5,0.5] |
| dependency_batches_0 | [0,0,0,0] | [0,1,0,0] | [−0.25,0.75,−0.25,−0.25] |

这直接补充了此前“全零奖励、无法产生更新”的诊断：在相同初始权重和工具下，
提示附录能让这两题出现混合 Reward。三个成功分别属于两个家族，均有真实公开
测试失败后继续生成新 patch 的路径，原始步骤号已保留。该路径是行为描述，未对
首个 patch 重新运行私有判据，也未做隐藏反馈消融，不能称为反馈因果证明。

示例组 Token 增加约 59.8%，工具调用和 patch 也更多，**没有证明工具效率改善**。
原提示 5 条 Loop 为 finished 但全部独立失败；示例组的 3 个正确候选中，2 条 Loop
因请求前 Token 预算保护而 failed，1 条因步数额度终止。成功按独立判据认定，不
把 Loop 终态当作正确性，也不能据此宣称完成/终止策略已经解决。

示例组两次 model_call_failed 均是请求提供商前的预算拒绝，采样 start/return、
原始 Token 与财务回执完整。通用 Trace 的未知 usage 占位与真实付费调用分开，
本轮 unknown provider usage 为 0；没有超出预声明的实际 Token/步数/工具额度。

### 费用与核验

本轮采样估计 0.872 元，加初始化/元数据保守额度 0.050 元，共 **0.922 元**，
低于 1.50 元上限。累计混合保守占用 **48.157 元**，可用 **1.843 元**，无 pending。
这些是保守估价，不是精确实付；未充值、使用重置卡或重做历史账本。

逐调用核对了 160 个财务 operation 的 Token 与估价；16 条 result、原始 turn、
候选及独立验证一致。17 项既有契约测试通过，范围为记录、计价、轨迹绑定及账本，
不冒充私有实验驱动脚本的新增单元测试。对当时 834 份本轮 JSON/JSONL/文本/CSV/log
使用实际 PyTRIO API Key 文本检测，无命中；结论仅限该检查范围，不代表全局安全验收。

### 数据入口

- 可提交的[汇总 JSON](../results/feedback-example-pilot-2026-11-08.json)。
- 本地 `run-01/protocol.json`、`queue.json`、`sources.json`、`source.tar.gz`：预声明
  参数、16 条队列及代码快照。
- `run-01/trajectories.csv`：每条 case、arm、seed、独立结果、Token、工具、耗时和费用。
- `run-01/analysis-01.json`：完整指标、分子/分母、组内 Reward/Advantage 与输入 hash。
- `run-01/verification-bindings.json`：run_id、case_id、判据来源/hash 与独立结果绑定。
- 各 run 目录：`tokens/`、`sample-started/`、`sample-returned/`、`trace.jsonl`、
  `candidate.json`、`patch-snapshots.json`、`verification/`、`financial-calls/` 及
  `artifact-manifest.json`。
- `completion-audit-01.json`、`financial-review-01.json`、`budget-before/after.json`、
  `execution-started/ended.json`：进程退出、检查范围及财务差值证据。

仅数值及封闭配置上传至 PRIVATE SwanLab，已回读确认成功数、完整分母、Token、
16/16 完成与 training_updates=0；原始 Prompt、代码和 Trace 留本地：

- [实验汇总](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/a4w868xq)
- [原提示](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/n7n9petv)
- [反馈示例](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/cs558ror)

## 对下一阶段的含义

结果支持继续研究“固定反馈示例提示下是否能训练”，尚未证明 RL 有效，不能直接
放行算法收益对照。后续 RL 必须将**同一示例 Prompt**同时用于初始 Base 和 RL，
不能把“RL+新提示”与“Base+旧提示”比较后归因于 RL。

当前训练桥接仍只接受 original 提示，不能直接把本轮示例轨迹塞入旧冻结训练队列。
若后续开展训练，需先在实验层显式绑定新的 Prompt profile/hash 并完成契约验收，
重新冻结开发/训练/留出方案、完整预算和 checkpoint；Agent Loop 无需修改。
本轮到此结束，不自动追加训练或以这两道开发题承担独立测试。

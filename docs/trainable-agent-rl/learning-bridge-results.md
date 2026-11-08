# 小仓库轨迹桥接：离线接口与真实容器控制

日期：2026-11-07，依据[ADR0043](../adr/0043-learning-trajectory-bridge.md)。
本轮补齐LearningCase到TrainingBatch的数据接口，不启动真实provider采样、优化更新、
新任务readiness或算法比较。核心Loop、旧minirepo runner、工具、判据和财务账本均未修改。

## 实际接口与证据链

新增`learning_rollout.py`，显式注入已有SamplingBackend和ProcessRunner：

| 接口 | 输入 | 输出及拒绝条件 |
| --- | --- | --- |
| `run_learning_rollout` | LearningCase、backend、PolicyBinding、run/seed、工作目录和固定镜像 | 复用旧runner；固定原Prompt、八项读取及既有预算；不创建ServiceClient |
| `seal_rollout` | 已结束的run目录及预期case/policy | 先检查原始数组、Trace、独立候选判据，再原子发布独立绑定；原文件不覆盖 |
| `load_rollout` | case、目录和预期policy | 返回typed LearningRollout；文件、版本、身份或终态不一致则拒绝 |
| `assemble_group` | 四个已验证LearningRollout | 仅同train case、同policy/tokenizer/Harness、不同run/seed的provider轨迹可形成G4 |

原始记录仍由RecordingModel/FileRecordingSink保存：真实prompt/completion Token、
old logprobs、原始文本、seed、采样参数。不会把解析后的Tool Call重新编码成训练Token，
也不修复或删除非法动作。已有`build_batch`负责右移target，仅本轮completion有直接
loss权重，历史assistant/工具Observation作为prompt位置权重为0。

新增外围backend记录`sample-started/turn_*.json`和`sample-returned/turn_*.json`，
分别在进入sample适配器前、适配器返回后原子保存。返回记录绑定原始Turn摘要；
进入、返回、Raw Token及Trace完成事件必须逐次一致。进入记录不是“provider已收费”
证明：适配器可能在RPC前拒绝，未知请求也可能已收费；任何未返回或缺失Raw的情况
均保留证据并拒绝训练，不自动重试或释放费用。

若Trace有失败调用，但所有实际进入该边界的调用均返回且Raw完整，才把其余失败
标作本地采样边界之前的失败。原Trace的`usage_known=false`保持不变；新派生解释
不会将原未知字段改零。历史轨迹没有这些记录时不补造receipt，也不重标为新任务。

## 独立版本与失败结果

绑定文件为`learning-binding/turn_000.json`，当前`schema_version=2`，包含可直接读取的：

- case ID、split、family_group、dataset、公开反馈与私有criterion版本。
- 公开fixture、reference和私有checker的hash，以及原始artifact逐文件hash。
- policy ID、snapshot/tokenizer摘要，以及`provider`或`scripted_control`来源。

旧runner的result仍保留`development/minirepo_dev_v1`，没有把旧数据改成train/test。
新运行器及分析必须读取本绑定；不能用旧标签推断实际数据版本。
绑定v1仅保留在本轮早期控制证据及其source archive，当前入口要求v2，不静默补字段。

成功取自实际候选hash关联的独立verdict，不取自模型结束声明。缺少候选时只有既有
可审计的工作区违规、捕获失败或容器终止规则可确定失败；结果与检查执行覆盖分开。
预算耗尽、failed/cancelled等终态不会从组内奖励分母删除，实际动作Token仍保留。

从Trace终态读取Loop时长、步数、Token和工具计数，与原结果/Raw核对；检查12步/
12工具、36k累计Token、3072生成、6144单请求总长、单次1024及180秒Loop上限。
已知超量保留success、完整消耗和budget_violations，阻止训练组装，不能把成功改写成
预算满足。缺少终态预算证据也不能通过。E2E准备/验证时间不冒充Loop墙钟。

G4奖励仍为独立0/1成功，Advantage=Reward−组均值。组内不能混任务、snapshot、
tokenizer或Harness，也不能移除失败。全组没有采样Token时返回`batch=None`，保留
全部Reward和Advantage供上层记录，不制造训练动作；新调度器需显式记录这种跳过原因。

## 固定脚本的真实容器控制

正式控制证据为`learning-bridge-v1/control-03/`：使用新train家族table_join变体0，
所有backend均为`scripted_control`、合成Token ID/logprob，不连接模型服务。

| 控制 | 实际结果 | script backend采样次数 | 检验目的 |
| --- | --- | ---: | --- |
| interactive | Patch1后公开测试exit1；Patch2后exit0；独立通过 | 5 | 反馈、第二次编辑、独立Reward和Raw完整链路 |
| single | 两个缺陷同次Patch修复；协议合成停止；独立通过 | 1 | 合成停止没有虚构采样Token |
| unchanged | Loop finished，独立失败 | 1 | finished不等于任务成功 |
| empty | 上下文在采样前拒绝；Loop failed，独立失败 | 0 | 零采样失败保留，不生成Raw或梯度 |

四条都执行了独立候选检查；脚本轨迹均被provider训练组装入口拒绝。公开失败信息
进入后续脚本请求，完整私有checker未进入请求；检查范围不扩张为通用Secret检测。
本组结果不能计算模型修复率，也不能说明4B会利用反馈。

初次控制01之后补充独立预算检查，控制02之后将绑定升级为显式版本字段的v2；
各目录与源码归档保持原样。control03是在最终代码下重新执行的四项组件控制，
不是三次模型实验或对同一模型队列补样本。

边界契约还覆盖：非法动作的原始Token/mask；不同policy、seed、版本、tokenizer；
缺失Raw、未返回采样、Reward矛盾、封存后变更、重复身份、跨case/test混入及脚本拒绝。
已知超量测试使用人工构造的unit输入：成功和6146 Token被保留，request_tokens超限
阻止训练；这不是一次provider消耗。真实控制四条都在受检额度内。

## 已完成与下一阶段

当前完成的是typed输入、独立结果关联和本地TrainingBatch组装。还未完成新MiniTask
任务的SDK sampler/snapshot身份绑定、完整训练/评测调度、durable更新恢复及真实
provider数值Smoke。旧函数任务的训练器/恢复探针可复用，不等于新链路已验收。

PolicyBinding由可信实验控制器提供。类型、hash和receipt能证明内部一致性及文件
未变，不能认证任意外来文件确由远端策略采样；后续控制器必须绑定实际SDK客户端、
保存的snapshot与tokenizer，并在首次调用前通过完整财务准入。

本轮provider采样/训练调用均为0，没有租GPU、充值、使用重置卡或上传新云端数据。
账本仍为**42.667/50元**、可用**7.333元**、无pending。
完整80条readiness/训练/评测草案保守分配19.702元，资金缺口12.369元保持；不得只因
桥接通过就先运行readiness。目标“交互学习收益→算法比较”仍未完成。

下一步只准备新任务的离线调度、恢复与完整准入；为用户评审准备可执行、可审查的
完整队列后，再决定预算，不自动提额或启动模型。

本地根目录`benchmark-results/trainable_agent/learning-bridge-v1/`保留control01/02/03、
对应source归档、read-only review、质量和状态证据。离线控制只写全新目录：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_bridge_probe \
  --output benchmark-results/trainable_agent/learning-bridge-v1/control-new
```

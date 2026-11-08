# 交互可学习性：开发执行记录

> 后续更新：已完成[两个已结算小时的覆盖校准](closed-hour-reconciliation.md)，当前保守累计
> 45.909元、可用4.091元，仍为50元上限。32条对照已获原预算内准入；下文的0/32及
> not_admitted保留为首次准备时点。当前运行与结果以新目录paired-01及后续结果报告为准。

> 对照已结束：[完整结果](interaction-learnability-results.md)。32/32完成，3/16对4/16，
> +6.25pp的95%探索区间[-12.5,+25]pp；反馈恢复门禁未通过。累计保守48.021元，
> 剩余1.979元、pending=0，未开始新训练，目标继续进行。

日期：2026-11-07；分支experiment。依据用户新目标及[ADR0037](../adr/0037-interaction-learnability.md)，
继续实施[工具冷启动设计](tool-cold-start-plan.md)。设计文档和旧报告作为历史版本保留。

**状态：工程准备通过，真实模型对照0/32执行，资金未准入；目标尚未完成。**
确定性控制测试不能证明模型学会了多轮修复，也不能代替后续RL算法比较。

## 实际代码变更

| 模块 | 当前行为 | 验证 |
| --- | --- | --- |
| minirepo_prompts.py | 固定original/tool_example两种Prompt；只追加已评审示例，不读私有判据 | 两种历史mode的原Prompt hash保持一致；示例与评审文档字节一致 |
| minirepo_rollout.py | 显式prompt_variant，默认original；每run保存variant和instruction hash | 原Loop不改；两个variant均完成真实Docker内失败反馈后再次修复 |
| minirepo_experiment.py | --prompt-study生成8题×2seed×2Prompt完整队列；两组均interactive | 16配对同case/seed、交替顺序；示例必须有控制探针，单次输出固定1024 |
| minirepo_prompt_probe.py | 从示例解析真实动作并执行玩具修复 | 首次修改后公开测试exit1，再修改后exit0，最终独立验证通过；新模型调用为0 |
| minirepo_analysis.py | 按prompt_variant配对；检查队列/结果/arm及case/seed绑定 | 两组各16分母；错误arm绑定被拒；首次Patch复评写入派生输出旁，不写原始目录 |
| diagnostics/failure_review/publish脚本 | 支持original/tool_example分组，保留既有workflow入口 | 无用量时比例null，未把未知计零；SwanLab只允许汇总白名单，尚无新模型结果上传 |
| 费用准入 | 完整队列预留优先，拒绝后不连接服务 | 实际准备被拒；共享账本hash不变；32行均not_run |

所有源码位于独立`experiments/trainable_agent/`，主Runtime不引入训练依赖。
模型可见工具仍只有read_file/search_repo/apply_patch/run_tests，权限与Docker隔离不放宽。

## 已验证证据

私有根目录`benchmark-results/trainable_agent/interaction-learnability-v1/`：

- `control-02/control-summary.json`：示例五个动作的envelope/Schema与实际执行通过；
  原Loop接收失败测试反馈后执行第二次Patch，独立验证通过。policy明确为scripted_control_only，
  合成Token只用于契约测试，scientific_rollouts=0，不进入RL或云端训练曲线。
- `task-gates-01/summary.json`：原8题参考、原bug、常数错解、逐缺陷mutation、公开测试，
  56项真实Docker检查通过。任务内容与原开发集相同，没有换题凑分。
- `rendering-01.json`及同目录`check-rendering.py`：只使用ModelScope本地缓存，tokenizer
  vocab/template hash与历史冻结身份一致。8题×2Prompt共16种初始渲染全部满足上下文；
  示例组输入1311–1339 Token，加1024输出≤6144。原Prompt与历史16条interactive首次
  请求逐Token一致。Hugging Face路径缓存缺失后改用SDK实际采用的ModelScope缓存，
  未下载模型、创建服务client或发起采样。
- `legacy-analysis-cap1024-01.json`：新分析器重放历史已结束数据，policies、配对差/区间、
  discordance及机制门禁与原报告一致；首次Patch重新评分在新派生目录，不改原评分。
- `paired-preparation-01/`：冻结新protocol、源码tar/hash、32行queue及admission。
  `preparation-status-01.json`记录0执行、0模型调用、0更新、成功率null、未连接模型/SwanLab。
  未执行机会不从32分母删除，也不伪装成32次模型失败。

`control-01`为较早源码下的探针，保留历史；当前准备绑定control-02。旧阶段的source归档
与Trace不改写；工作树更新后，旧运行须在其归档源码下重建，不能把新代码当旧实测版本。

## 固定实验与准入

沿用原候选协议：同初始4B策略、temperature1/top_p1/top_k−1；每题2seed，原Prompt与
示例Prompt同期运行。12步/12工具、36000总Token、3072总输出、每轮1024、6144上下文、
180秒不变。完整32条最大预留2.88元，不复用历史A组替代新同期对照。

真实评测完成后，以独立成功率、配对差与家族bootstrap为主，协议拒绝、成功Patch、修改后
测试、工具数、失败在内Token、端到端时间与成本另报。机制准入保留正配对差、至少2个
tool_example-only成功配对，以及至少2家族有“公开失败测试→再修改→独立通过”代理。
这些是开发启发式，不是收益显著性标准。只有4家族，退化区间不证明等效。

若通过，才冻结新家族训练任务上的success-only Pilot，随后比较RL算法；必须为完整Base/RL
或算法对照留足评测额度。若只改善格式，下一项任务/预算/信息利用假设另立协议，不能
宣称交互问题已经解决。未生成新Train24/Test16，未开展新的GRPO/GSPO效果比较。

## 当前资金状态与下一步

当前累计保守48.155元，含原不确定性预留；剩余1.845元，pending=0。2.88元完整评测
需要至少累计51.035元容量，因此50元门禁正确拒绝。¥3.37只是旧时点已入账观察，
没有完整账期和归属，不能用来重置当前消费或证明本轮实付只有该数。

下一步取决于可审计账单释放已证明的过度预留，或用户明确调整累计预算。
若仅批准当前对照，累计52元可容纳当前保守48.155+2.88=51.035；这是准入算式，
不是预计实付，也没有自动修改限额。额外RL/算法比较还须独立资金准入，不能挤占评测。
当前`CalibratedBudget`仍硬绑定50；未来预算调整须保留旧ledger/父hash/operation ID并
完成迁移契约测试，不能只改JSON的limit。目标保持active，资金问题不解释为研究已完成。

## 可复现入口

以下命令使用新输出目录；前两条无模型费用：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_prompt_probe \
  --output benchmark-results/trainable_agent/interaction-learnability-v1/control-new
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_gates \
  --output benchmark-results/trainable_agent/interaction-learnability-v1/task-gates-new
```

配对评测入口已实现；当前资金不足，执行会在服务连接前拒绝，不应重用已完成准备目录：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_experiment \
  --prompt-study --max-completion-per-call 1024 \
  --prompt-control benchmark-results/trainable_agent/interaction-learnability-v1/control-02/control-summary.json \
  --gates benchmark-results/trainable_agent/interaction-learnability-v1/task-gates-01/summary.json \
  --quote benchmark-results/trainable_agent/minirepo-v1/service-preflight-02.json \
  --output benchmark-results/trainable_agent/interaction-learnability-v1/paired-new \
  --swanlab-mode online
```

源文件变更须重做绑定的控制探针与任务检查；首轮精确渲染也须复核。执行前重新核对服务
报价和当前ledger；未知RPC不重放。工程门禁结果另存quality-gates与输入hash，不以数量
替代上述覆盖范围。完整Memory/Skills、恢复、Secret与网络攻击矩阵本阶段未重验。

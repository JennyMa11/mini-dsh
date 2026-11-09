# 固定规模 Base / GSPO 效果验证 v1

2026-11-09；状态：已完成，352/352条真实轨迹已复核。决策：[ADR0056](../adr/0056-gspo-efficacy-study.md)。

用户要求按历史 GRPO 规模验证 GSPO 相对 Base。此轮复用原 expansion_v2 函数修复题，
不混入后续多文件数据、示例注入或候选策略修订。原24家族96题、train48/validation16/test32
按家族隔离，全部336项参考解、错误解、变异与边界检查证据保持原版本。

| 部分 | 固定规模 | 策略与顺序 |
| --- | --- | --- |
| 训练 | 40个G4组，160条 | 原 pilot seed17 shuffle 顺序；采样 seed=17+slot*100+repeat |
| 验证 | 16题×2重复×2策略=64条 | 原 evaluation_queue，Base/GSPO交替顺序、同题同seed配对 |
| 测试 | 32题×2重复×2策略=128条 | 原 evaluation_queue，Base/GSPO交替顺序、同题同seed配对 |

合计352条。40是尝试组数，实际更新由混合奖励决定，不强行凑足历史28次更新。
失败、预算终止、全0/全1奖励保留；连续10组零信号停止，不补采挑题。完整训练后固定
终点，无论验证成功率方向如何都运行全部配对测试。未完成队列不得只报告已跑样本为完整效果。

Qwen/Qwen3.5-4B、LoRA r16、seed17、MLP/attention、无unembed；Adam lr1e-5、
beta1=.9/beta2=.95、grad norm1、无decay。温度1、top_p1、top_k=-1。
每轨迹8步/8工具、24000总Token、2048总生成、每次请求最多4096；固定原提示、工具与独立二值奖励。
原始采样Token/logprob用于训练，工具观察与Prompt无梯度。原始大产物仅本地保存。

变体为 `gspo_multiturn_wide_clip_v1`：完整多轮轨迹的assistant Token几何平均重要性ratio，
每轨迹等权，clip=.2/.2，组内中心优势，不做std归一化或KL。PyTRIO 0.2.9自定义loss按
已测CE sum还原，不乘Datum数量。历史真实数值探针的五个算法源码hash必须与当前完全一致，
否则拒绝复用；每个新有效组还要核验当前logprob、活跃裁剪不超过5%、有限梯度与事务回执。
这不是论文默认窄裁剪参数的精确复现，也不与历史GRPO不同损失归一化强作因果排名。

Base从此次初始零LoRA采样，初始化实际权重必须与历史seed17初始归档差0；GSPO来自第40尝试组
终点。每个有效更新保存optimizer+sampler、日志及回执；未知请求停止，不重新提交更新。
原先两个已终止初始化请求的隔离证据和原预留保留，其他未知请求继续阻断。
只保留运行器必要内部资源准入，不增加账单核查或用量导出。

入口：

```sh
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_study_run prepare --directory benchmark-results/trainable_agent/gspo-efficacy-v1/run-01
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_study_run run --directory benchmark-results/trainable_agent/gspo-efficacy-v1/run-01
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_study_analysis --directory benchmark-results/trainable_agent/gspo-efficacy-v1/run-01 --output docs/results/gspo-efficacy-v1-2026-11-09.json
```

主要终点为test Base与GSPO各64次的独立测试成功率、配对差值与8家族聚类bootstrap区间
（10000次，seed20261106，nearest-rank）。验证各32次单列，不合并制造更大测试样本。
重建全部group奖励、mask、datum hash、当前logprob的loss及有效更新回执链；最终实际权重须改变。
旧测试集已使用过，本轮为预声明固定基准复测；单训练seed和小家族数限制外推。
只有点估计改善不称稳定收益，区间跨零仍须如实报告；不能以旧GRPO成绩证明算法优劣。

执行目录：`benchmark-results/trainable_agent/gspo-efficacy-v1/run-01`。源码快照与输入hash见
`plan.json` / `source.tar.gz`；容器门禁见同级`security-before-01/result.json`。
执行前根项目1072测试与实验包355项全量回归通过（包含新增调度/恢复/数值审计9项）。


故障及一次恢复补充：run-01在第31组反传查询时失败，原任务owned/ENDED，
原124条轨迹、30组/19次有效更新和未知反传预留保留。按
[ADR0057](../adr/0057-gspo-terminal-backward-rollback.md)只允许一次新目录run-02恢复：
加载最后完整optimizer检查点，实际权重与非空Adam状态全等后，重建失败组的独立更新；
不重新采样，不改变352行队列和评测终点。恢复验证失败即停，不递归尝试。


## 完整结果（2026-11-09）

40组/160条训练、25次有效更新、15组零信号跳过；64验证+128测试全部完成。
验证Base 10/32→GSPO 18/32（+25pp）；测试Base 28/64→GSPO 33/64（+7.8125pp），
95%家族聚类探索区间[-1.5625,+17.1875]pp，包含0，稳定收益尚未证实。
完整训练回执、原始loss/mask与恢复一致性复核通过，训练后实际权重差>0。
[完整报告](../results/gspo-efficacy-v1-2026-11-09.md)。
最终目录为`benchmark-results/trainable_agent/gspo-efficacy-v1/run-03`；run-01/run-02停点保留。
离线生成报告：

```sh
uv run --project experiments/trainable_agent --extra lab python experiments/trainable_agent/scripts/gspo_efficacy_report.py
```


## 本阶段证据覆盖

| 能力 | 离线契约证据 | 真实证据与边界 |
| --- | --- | --- |
| 与GRPO同规模固定队列 | `test_frozen_queue_matches_old_grpo_scale_and_order` | plan固定352行，实际352/352，未重采原124条 |
| 不按验证方向筛选测试 | `test_complete_40_groups_and_all_evaluation_even_with_zero_validation` | 完整64验证+128测试，固定最终策略 |
| 零信号跳过 | `test_zero_signal_skips_optimizer_and_retains_entire_declared_queue` | 15个组journal=skipped，全部训练样本保留 |
| 未知事务不重放 | `test_unknown_optimizer_is_never_replayed` | 原反传查询故障保留并终止旧Actor；未知optim故障仅契约测试，真实未触发 |
| 整阶段准入先于SDK | `test_provider_factory_not_created_before_stage_admission` | 本轮准入通过；真实资源耗尽未触发 |
| 完整训练优化器恢复 | `test_only_terminal_backward_may_rollback`、`test_nonempty_optimizer_tensor_equality_and_steps` | 实际恢复LoRA差0、非空Adam state及param_groups全等 |
| 大检查点就绪 | `test_checkpoint_upload_readiness_is_polled_with_deadline` | 实际187MiB归档上传/下载后复核；修正版5分钟耗尽仅契约覆盖 |
| 损失与原始轨迹绑定 | `test_raw_gspo_loss_and_prompt_mask_audit` | 全40组Datum重建、25组loss/梯度掩码重建通过 |

前后容器门禁验证网络关闭、无宿主挂载、凭据环境隔离、哨兵文件不变、超时/取消后容器移除；
这是特定探针范围的独立检查，不代表Harness全部安全功能已验收。实验结束未发现遗留minidsh容器。
报告两次离线生成JSON及Markdown hash完全一致；见本地`report-reproducibility.json`。

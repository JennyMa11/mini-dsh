# ADR 0056：按历史 GRPO 规模验证 Base 对 GSPO

- 日期：2026-11-09
- 状态：Accepted（用户明确要求按 GRPO 规模验证 GSPO 相对 Base 的提升）

## 科学范围

独立实验包复用原函数级 expansion_v2 数据与执行器，保持 Qwen3.5-4B、LoRA r16、
seed17、G4、二值独立测试奖励、提示、工具、采样与任务预算。固定与历史 GRPO
相同的 40 组/160 条训练任务顺序和 seed；固定 64 条 validation、128 条 test，
Base/GSPO 各 32 次 validation、64 次 test。Base 为此次训练初始零 LoRA，GSPO
为固定第40尝试槽终点，不按 validation 选 checkpoint 或决定是否打开 test。

使用已经验收的 gspo_multiturn_wide_clip_v1：整轨迹几何平均 ratio、序列裁剪
[0.8,1.2]、轨迹等权、组中心优势、无std/KL。只评价这一个明确变体，不声称
精确复现论文窄裁剪。旧归一化探针按 gspo.py/gspo_trainer.py/datum.py/contracts.py/
gspo_numeric.py 与固定 SDK/锁文件核验后复用；不重跑已知存在初始化404风险的
旧 restore 探针。真实权重、回调数值、未知事务门禁持续生效。

旧 test 已打开；此轮是冻结既有 benchmark 的预声明复测，不称为新的盲测。
本轮主要比较 Base 与 GSPO，不直接将不同归一化/时间的历史 GRPO 分数作为
算法优劣证据。历史数据/分数保留，新的原始轨迹使用独立目录。

## 执行与资源

按原 train_v2 的完整组准入策略，以及 ADR0048 的确认阶段策略，固定352行科学队列。
继续唯一累计150/新增100资源epoch，不重置账本；以完整下一G4的保守上界加全部
尚未运行的配对评测与收尾预留做阶段准入。资源不足则停止，完整未运行分母保留，
不以未来可能释放预留或预期零信号作为资金来源。用户已要求不做额外用量核对，
因此只保留运行器必要的内部限制，不开启账单UI、额外计价审计或用量导出。

复用 ADR0053/0054 已验证的两条 terminal-init 隔离证据：原 pending 金额和状态
不动；只有这两条旧初始化请求不阻断新执行，任一当前任务未知请求立即停止。
新任务使用 fresh seed17 初始化、实际初始零LoRA/参数核对，避免重放旧恢复请求。
每个有效更新保存 optimizer+sampler；仅有完整回执才可有界恢复，未知 optim 不重放。

## 结论门槛

固定40槽及全部配对评测完成后，主要报告 test Base/GSPO 成功分子/64、配对差值、
8家族 bootstrap 10000次/seed20261106/nearest-rank区间；validation独立列出。
观察到正向点估计只称探索性变化；区间跨0或仅单训练seed时，不宣称稳定收益。
完整原始 Token/logprob、mask、group优势、归一化数据、独立判据、数值回调与
checkpoint事务都需离线重建；零信号组保留且不补采。停止时不把部分成绩当完整结果。

## 执行结果

352/352完成，25次有效更新；测试Base28/64、GSPO33/64，+7.8125pp，
95%探索区间[-1.5625,+17.1875]pp。按ADR0057恢复一处终止Actor反传查询故障，
未重新采样，原始失败保留。见[完整报告](../results/gspo-efficacy-v1-2026-11-09.md)。

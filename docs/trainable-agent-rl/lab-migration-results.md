# Lab GSPO 迁移结果与 SwanLab 记录

日期：2026-11-07。实现方案见 [迁移设计](lab-migration.md)，决策见
[ADR0035](../adr/0035-lab-gspo-swanlab.md)。本阶段已结束，不继续付费训练。

## 实际完成

参考 Lab 固定源码版本 `238bc688ca7d5c994c580f732ee9c2c92483bd20`，在独立
实验包接入多轮 GSPO、PyTRIO custom-loss、数值门禁、SwanLab 指标与历史回填。
核心 Agent Loop、工具和插件架构没有因此修改。

已上传到 [SwanLab 私有项目](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl)：

| 记录 | 用途与核验 |
| --- | --- |
| [历史 GRPO 训练](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/r8ouh55k) | 原40组/160条训练曲线，28次非零更新；标记历史导入 |
| [历史 Base 测试](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/8t5eka3u) | 28/64；云端回读与本地一致 |
| [历史 GRPO 测试](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/l26wlf3d) | 34/64；云端回读与本地一致 |
| [历史差值及区间](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/7elrjsua) | +9.375pp，95% family bootstrap [-10.9375,+26.5625]pp；不宣称已证实收益 |
| [首轮探针失败](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/112uf52p) | 实际缩放不匹配，门禁停止；没有开始 Coding 训练，保留失败记录 |
| [GSPO Smoke](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/42jx7bz5) | 数值复测通过、8条轨迹、2次非零更新；状态 FINISHED，并回读核对 |

项目 `visibility=PRIVATE` 经 API 确认。只上传数值指标及封闭配置，没有上传
原始 Prompt、Token、Trace、候选代码、参考解、私有验证器、凭据或服务 checkpoint URI。
全部事实源保留在本地；SwanLab 是派生视图，历史回填不产生模型调用。

## 数值问题与修正

最初按历史 PPO 的 `sample_mean` 对 custom-loss 乘 Datum 数进行补偿。
远端实际 LoRA 导出显示 duplicate 更新恰为 single 的2倍，门禁判失败，
阻止 Coding 训练。该次保守费用 **¥0.456** 没有释放或清零。

固定 PyTRIO 0.2.9 的该 custom-loss CE 路径应按 **sum** 处理。
修正后从同一初始 optimizer state 分别恢复，single/duplicate 更新范数分别约为
`4.85144e-8 / 4.85144e-8`，比值 `0.9999999999999999`，通过预声明≤3%误差门禁；
Prompt/Observation 的直接 Loss 梯度为零。数值探针使用较大 optimizer epsilon
近似线性更新，其更新范数不用于衡量主训练幅度。

正式工程组另创建全新的初始 LoRA，rank16、seed17、LR1e-5、G4，组内居中
Advantage、等轨迹平均、Reward=独立验证0/1、无KL、无补采。这里使用明确命名的
`gspo_multiturn_wide_clip_v1`（裁剪0.8–1.2），不声称精确复现 Lab 原配置。

## Coding Smoke 与审计

| 项目 | 实际结果 |
| --- | --- |
| 固定工程任务 | `smoke_last_even_0`、`smoke_defaults_0`；每题4条，训练前已固定 |
| 完成/声明轨迹 | 8/8；独立验证通过4/8，仅为训练诊断 |
| 两组 Reward | `[1,1,0,0]`、`[1,0,1,0]` |
| 非零更新 | 2次，均保存 optimizer state 和 sampler snapshot |
| 逐轮 Datum | 32 + 28 = 60；保持各自真实 Prompt 和旧 Logprob |
| 推理 Token | 79,358，保留失败轨迹消耗 |
| Assistant Loss Token | 3,868 |
| 工具调用 | 54；没有据此宣称工具效率提升 |
| 双计算 padded Token 上界 | 262,616，包含初始 Forward 与后续 Forward/Backward |
| 主训练实际 adapter 参数差范数 | 0.0659668437；initial/final 完整导出留在本地 |
| Fresh-policy 宽窗口活跃裁剪 | 两组均为0 |
| 同批次 Lab 默认窄窗口诊断 | 第1组0，第2组25%；仅诊断，没有用其更新 |
| Loop 终态 | 5条 `budget_exhausted`、3条 `finished`；与修复正确性分别统计 |
| 成功尝试墙钟 | 456.83秒，含数值复测、远端等待与权重下载，不是 GPU 时间 |

独立后处理从原始 Token 重建每组优势、整轨迹 GSPO Batch/hash，逐条核对
run/policy/seed、候选 hash、私有验证结果和 checkpointed 事务，复算所有
Prompt/Observation 位置的直接梯度为零，8/8通过审计。
`finished` 不等于修复通过；预算终态也不能从预声明分母排除。

运行结束时训练源码 hash 全部未变，源码压缩包与 sidecar 保留。
之后仅改善了费用收尾点、分组/阶段 Token 指标命名；没有改变 Loss、采样、
Reward 或 optimizer。差异记录在 `post-observability-01.json`，已运行结果仍绑定
归档源码。旧 GRPO 整份测试分析用原冻结源码再次离线重放，与原 JSON 完全一致。

## 费用与结论边界

| 保守费用类别 | CNY |
| --- | ---: |
| 两轮数值探针管理预留 | 0.900 |
| 两轮数值探针双计算 | 0.008 |
| 全部采样（含两条短文本探针） | 0.204 |
| 主 GSPO 双计算 | 1.194 |
| 主训练管理预留 | 0.730 |
| **本轮合计** | **3.036** |

累计预算估算 **¥41.163 → ¥44.199**，含原有¥2不确定性预留，无pending，
累计¥50上限剩余¥5.801。本轮¥5阶段额度包含失败探针，未重置账本。
当前服务端只读报价与既有价格快照一致。没有租用GPU或开通 SwanLab 付费套餐；
GPU 显存/时数未由服务暴露。这些数字是 Token 报价及管理费用预留的保守估算，
**服务商最终账单尚未对账，不能称为实际扣款**。

SwanLab 的 step3 补记最终收尾费用和阶段总 Token，来自不可变 summary/audit，
只做日志补记，没有恢复 optimizer 或启动新训练。当前历史 Smoke 的 step1/2
`usage/total_tokens` 表示组消耗；新控制器改用 `usage/group_tokens` 与
`usage/stage_total_tokens`，避免和历史训练累计曲线混淆。

这次证据支持“多轮 GSPO 工程链路可执行、Mask 与缩放通过数值检查、参数确实
更新且费用受控”。它**不支持 GSPO 比 GRPO 更好或工具效率改善**：只有两个工程任务、
一次训练 seed，没有新的 Base/GRPO/GSPO 配对 held-out 评测，也未验收自动续训。

下一阶段先补匹配轨迹归一化的 token-PPO 对照，固定新开发/留出任务、采样种子与
完整费用上限，再评估8组/臂的开发 Pilot。当前阶段已关闭，不能按规划中的
¥8–12预测自动扩额或直接复用已打开测试集调参。

## 本地证据与复现

私有 artifact 根目录：`benchmark-results/trainable_agent/lab-migration/`。

- `gspo-smoke-01/`：首轮数值失败、原始源码、LoRA导出和事务费用。
- `gspo-smoke-02/`：protocol、source归档、8条轨迹、checkpoint、summary、审计与云端回读。
- `history-01/`、`historical-comparison-01/`：脱敏导入副本与云端URL。
- `cloud-readback-01.json`、`legacy-test-replay-01.json`：历史指标回读和完整旧报告重放。
- `service-preflight-01.json`：本轮只读报价；`post-observability-01.json`：收尾后的指标改动。

不产生模型调用的复核命令（输出路径必须全新）：

```bash
uv run --project experiments/trainable_agent --extra lab python \
  experiments/trainable_agent/scripts/audit_gspo_smoke.py \
  --directory benchmark-results/trainable_agent/lab-migration/gspo-smoke-02 \
  --output benchmark-results/trainable_agent/lab-migration/replayed-audit-new.json
```

源码静态检查、类型检查、契约/集成测试结果及关键输入 hash 保存在同名
[结果 JSON](lab-migration-results.json) 和私有 artifact 清单；测试数量不替代上述验收范围。

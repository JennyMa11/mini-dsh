# Agentic RL Lab → Mini-dsh：GSPO 轻量迁移

日期：2026-11-07。边界见 [ADR0035](../adr/0035-lab-gspo-swanlab.md)。
当前阶段已完成工程链路验收：[实际结果](lab-migration-results.md)。新增保守估算¥3.036，
累计¥44.199；后续训练规模须重新通过阶段门禁，累计预算仍为¥50。

## 1. 选择依据

实际阅读的上游固定于
[`238bc688ca7d5c994c580f732ee9c2c92483bd20`](https://github.com/datawhalechina/agentic-rl-lab/tree/238bc688ca7d5c994c580f732ee9c2c92483bd20)。
核心参考 [GSPO Loss](https://github.com/datawhalechina/agentic-rl-lab/blob/238bc688ca7d5c994c580f732ee9c2c92483bd20/experiments/gspo/loss.py)、
[训练入口](https://github.com/datawhalechina/agentic-rl-lab/blob/238bc688ca7d5c994c580f732ee9c2c92483bd20/experiments/gspo/train.py)、
[依赖](https://github.com/datawhalechina/agentic-rl-lab/blob/238bc688ca7d5c994c580f732ee9c2c92483bd20/pyproject.toml)
和 [实验说明](https://datawhalechina.github.io/agentic-rl-lab/experiments/gspo/)。
本机检查了 PyTRIO 0.2.9 的 `client/training.py::_forward_backward_custom`；
SwanLab 使用与上游一致的 0.10.4，CPU Torch 固定 2.9.1。

| 候选 | 与当前代码的适配 | 本轮决定 |
| --- | --- | --- |
| GSPO | 已有组采样、Reward、Token/Logprob 与 GRPO 基线，可独立替换 Loss；无需额外模型 | 迁移多轮版本，先验证数值与费用 |
| [ReTool](https://datawhalechina.github.io/agentic-rl-lab/experiments/retool/) | 多轮工具推理很贴合，但 Mini-dsh 已有工具 Rollout 与 Assistant Mask，新增算法区分度有限 | 保留工具反馈/监控经验，不搬数学 Sandbox |
| [DAPO](https://datawhalechina.github.io/agentic-rl-lab/experiments/dapo/) | 动态采样有利于稀疏信号，但当前 40 组中已有 28 组非零，补采会增加开销 | 后续仅在零信号成为主要瓶颈时评估 |
| [AgentOPSD](https://datawhalechina.github.io/agentic-rl-lab/experiments/agentopsd/) | 多轮蒸馏需 Teacher/额外打分和新的变量控制 | 当前预算不引入 |

这是新算法的工程适配，不保证 GSPO 提升修复率。上游任务、样本量和模型训练
配置不同，不把其效果或金额当作本项目的实测。上游完整依赖集要求 Python≥3.13，
本项目仅迁移 Loss/训练接口思想，保持 Python 3.12，不引入数学任务集、视觉、
ALFWorld、模型权重下载或本地 GPU 训练。

## 2. 与现有 Harness 的连接

```
原 AgentLoop + 原 ToolRuntime/Permission/Docker
    → RecordingModel/Turn → 独立测试 Reward
    → build_gspo_batch → PyTRIO custom-loss → LoRA checkpoint
    → 独立评测
                 ↘ 白名单数值 → 本地 JSONL → SwanLab
```

复用 `rollout_v2.py`、`PricedBackend`、`Turn`、`training_arrays`、`TrainerV2.charge`、
`UpdateJournal`、`CalibratedBudget` 和独立验证器。核心 `src/minidsh/` 不作改动。
新增能力在 `experiments/trainable_agent/src/minidsh_rl/`：

| 文件 | 输入 → 输出；验收方式 |
| --- | --- |
| `gspo.py` | `tuple[tuple[Turn,...],...] + advantages` → `GSPOBatch`；再生成 SDK loss callback。检查右移、整轨迹聚合、零 Mask、零信号分母 |
| `gspo_trainer.py` | Batch + 冻结 client/config → 单次 custom backward/optim 与事务记录。两次计算预留；未知状态不重放 |
| `gspo_numeric.py` | 当前同一账本 → single/duplicate 远端更新、实际 LoRA 导出差值与门禁 JSON |
| `gspo_run.py` | 新输出目录、固定 Docker digest → 2组×4条工程轨迹、checkpoint、源码归档、费用与 Smoke 结论 |
| `tracking.py` | `RunSpec` + 白名单有限数值 → 本地版本化 JSONL；可选 SwanLab sink。远端日志故障不破坏事务 |
| `swanlab_export.py` | 冻结历史报告 → 标记 `historical_*` 的训练曲线、Base/RL 指标；不产生模型调用 |

PyTRIO 继续承担基础模型、LoRA、Forward/Backward、优化器和存储。Mini-dsh
实验层承担任务、隔离、轨迹、Mask、Reward、分组、算法目标、费用与独立验证。
CPU Torch 只对收到的 Logprob 小张量求梯度，不载入 4B 基础权重。

### 多轮 GSPO 目标

对轨迹 i 的所有轮当前 Assistant Token 组成集合 T_i：

`s_i = exp(sum(new_logp - old_logp for T_i) / |T_i|)`。

`L = -mean_i min(s_i*A_i, clip(s_i,1-e_low,1+e_high)*A_i)`。

每轮用实际采样 Prompt，保留历史 Assistant、Tool Observation 等作为条件，
只读取当前输出区间的 Logprob；没有把工具输出拼成被训练的 Completion。
不能按轮分别 clipping 后称为整轨迹 GSPO。

本轮沿用 `A_i = r_i - mean_group(r)`、Reward=独立测试通过的 0/1、G=4，
不除标准差，不加 KL、成本奖励或 Reward Model；所有轨迹等权，零信号轨迹
保留在分母，全零组跳过更新且不补采。

与历史 GRPO 的 Assistant-token mean 相比，这里还改变了轨迹权重；因此两个
历史 checkpoint 的差异不能单独归因于 sequence ratio。正式消融应新建
`token_ppo_equal_trajectory` 对照：每个轨迹内 Token surrogate 先平均，再平均
轨迹，保持相同 Advantage/裁剪/LR/预算，只有 ratio 与 clipping 粒度不同。
该对照当前尚未实现，也未执行收益实验。

### 裁剪和数值门禁

Lab 默认 e_low=0.0003、e_high=0.0004。已有跨引擎 Logprob 偏差远大于此量级，
直接套用可能使 Fresh-policy 样本梯度被裁掉。工程 Smoke 预先声明 e_low=e_high=0.2，
另记录 `gspo/paper_clip_fraction`，名称固定为 `gspo_multiturn_wide_clip_v1`。
不在看到任务成功率后选择裁剪值。

SDK 先远端 Forward，再本地 autograd，再将负梯度作为 CE 权重提交远端
Forward/Backward。必须实测该 CE 路径的归一化，不能套用历史 PPO 的 `sample_mean`。
首轮探针套用 Datum 数补偿后，duplicate 的实际更新为 single 的2倍，门禁
已阻止训练。当前修正为 **CE sum，不乘 Datum 数**；数学 Loss 保持轨迹平均。
再用全新目录、同一初始 optimizer 状态、
线性化 optimizer（较大 epsilon）测 single 与 duplicate 的实际 LoRA 更新，
比值须在 [0.97,1.03]，Prompt/Observation 的直接梯度严格为零。
失败探针的保守费用¥0.456保留在同一阶段¥5及累计¥50账本，不重新起算。
失败就停止，不能仅凭 SDK 返回成功判断缩放正确。

## 3. 当前工程实验及后续顺序

| 阶段 | 固定设计 | 通过标准与结论边界 |
| --- | --- | --- |
| 离线契约 | Ratio、跨轮聚合、Mask、裁剪方向、远端缩放补偿、异常输入、日志过滤、未知 RPC | 可执行测试全部通过；不代表真实训练收益 |
| 数值探针 | 1次≤8 Token 采样，1/2份相同 Datum 从同一状态各更新一次，3份 adapter 导出 | Mask=0、更新非零、更新比值误差≤3%；不做 Coding 成功率统计 |
| GSPO Smoke | 固定工程任务2个×G4=8条，最多2次训练更新；4B/rank16/seed17/LR1e-5 | 轨迹与独立 Reward 留存、至少1组非零、实际 adapter 改变、source hash未变、无pending、预算内 |
| 下一轮开发 Pilot（尚未授权扩额/未执行） | 先实现匹配归一化的 token 对照；每臂8组×4=32训练轨迹；固定开发任务8个×2种子×3策略=48评测 | Base + token对照 + GSPO；仅看开发信号、崩溃率、裁剪及费用，不能作新test收益结论 |
| 后续正式比较（未执行） | 按 Pilot 费用再分配；至少16个新留出任务，按family独立划分，先冻结完整队列和判据 | 按family配对 bootstrap、完整分母、至少2训练seed；新增费用先给明确上限 |

当前工程任务沿用已有 Smoke 的第2/4个任务和固定种子，不筛选历史测试题。
本轮不训练/复用已打开测试集作为新的 held-out 声明；后续新任务也需 reference
pass、broken fail、常数错解 fail 及 mutation 验证，私有判据不能进入可见工作区。
不依据当前两个工程组的结果宣称 GSPO 优于 GRPO。

## 4. 成本与停止条件

开始前同一账本保守累计 ¥41.163，含 ¥2 不确定性余量，可用 ¥8.837。
新增阶段上限 **¥5**，不是新增 ¥50，也不是实际已结算费用。

| 项目 | 资源上限与计算 |
| --- | --- |
| 数值探针 | 费用预留≤¥1；只有1条短文本采样；数个很短的训练样本；本地约3×65MB adapter归档 |
| Coding 采样 | 8条；每条8次模型/工具调用上限、总输入+输出24k、输出2,048；合计≤192k推理Token |
| 单组 custom训练 | 最多32个Datum×4,095输入Token=131,040 padded tokens；按两次计算261k计价，不假设Forward免费 |
| 2组训练最坏资源 | 两次计算合计≤524,160 padded tokens；全零组省掉该计算，不删除其采样费用 |
| 组准入 | 完整组保守≤¥1.70（采样≤¥0.2321、双计算约¥1.1907、optim¥0.01、snapshot/state各¥0.10） |
| 阶段准入 | 数值¥1 + 两组¥3.40 + 初始client/weights¥0.11 + closing¥0.30 = ¥4.81，留在¥5之内 |
| 时间/硬件 | 顺序单Agent；整阶段墙钟上限30分钟，已发出的RPC可能占满其超时；不租GPU。远端VRAM/GPU时数未暴露，不能声称已测 |

报价沿用已审计快照：每百万 input ¥1.49、output/training ¥4.54，忽略缓存折扣。
每类别向上取整至¥0.001，加 snapshot/optim 管理费保守预留（不是确认价目）。
计算为 `ceil_0.001(input*1.49/1e6) + ceil_0.001(output*4.54/1e6)`；
custom update 为 `2*ceil_0.001(padded*4.54/1e6)`。实际结算仍需服务商对账。
不因重置卡、已有账单较低或估算偏高而清零旧账本。

停止条件：未知 RPC 保留 pending；不重试 optimizer；数值探针失败；新策略
活跃裁剪比例>5%；无法预留完整组+收尾；墙钟超限；不一致的 tokenizer/source。
本轮结束后不自动花完剩余额度。未来两臂 Pilot 按历史分布粗估新增¥8–12，
受零信号比例、长度及自定义Forward收费影响，必须先与本轮实际费用重新核算；
该范围只是预测，不是已批准预算或精确报价。

## 5. SwanLab 使用与复现

已验证项目可见性为 `PRIVATE`：[项目看板](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl)。
原始本地证据是事实源；SwanLab 是可再生成视图。Smoke 的 `usage/total_tokens`
点表示当前采样组消耗，历史训练导入点表示累计消耗，分组/phase 已区分，不能
不加转换地混作同一累计曲线。上传曲线含 Reward、零信号组、
Token、更新次数、序列 Ratio/Clip、保守预算；评测包含成功分子/声明分母、覆盖率、
Token、工具次数与延迟。Token 未知不写0，无成功时每成功Token为null且不上传该点。
`verified` 表示任务结果可判定，并不意味着每条都运行了候选私有测试。

历史数据单独为 `historical_train/historical_test`，不得与 Smoke 的训练Reward
合并作 held-out 成功率曲线。训练Loss不能代替任务修复率。原始测试收益仍为
Base 28/64、GRPO 34/64，差值+9.375pp，family bootstrap 95%区间
[-10.9375,+26.5625]pp，不能宣称已证实收益。

```bash
# 从仓库根目录运行；核心默认安装不增加训练依赖
uv sync --project experiments/trainable_agent --extra lab
uv run --project experiments/trainable_agent --extra lab swanlab login --local

# 可先离线回填，确认后在登录状态下用 online 生成云端视图；输出目录必须全新
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.swanlab_export \
  --output benchmark-results/trainable_agent/lab-migration/history-new --mode offline

# 工程Smoke会实际付费，保留相同全局账本与阶段门禁
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.gspo_run \
  --output benchmark-results/trainable_agent/lab-migration/gspo-smoke-new \
  --image python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 \
  --swanlab-mode offline
```

不要把 API Key 放命令参数、配置、Prompt 或聊天中。SDK 登录凭据在本地，
`.swanlab/` 与 `swanlog/` 已忽略；只上传数字与封闭配置，不调用 `save()` 上传源码。
离线日志补传参照 [官方 sync](https://docs.swanlab.cn/en/api/cli-swanlab-sync.html)；
初始化/采集开关分别以 [init](https://docs.swanlab.cn/api/py-init.html)、
[Settings](https://docs.swanlab.cn/api/py-settings.html) 为依据并经安装版签名核对。
训练checkpoint恢复与 SwanLab resume 是不同事务；当前新入口不自动续训，
未知训练提交要人工审计，不能用看板 resume 来重放付费更新。

实际运行结果与云端链接另见本目录 `lab-migration-results.md`。

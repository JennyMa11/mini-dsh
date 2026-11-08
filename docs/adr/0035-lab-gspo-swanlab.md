# ADR 0035：迁移 Lab GSPO 与 SwanLab

- 日期：2026-11-07
- 状态：Accepted；数值与工程验收单独记录，不代表算法收益
- 授权：用户要求选择 Agentic RL Lab 实验迁移，并允许上传 SwanLab。

在 ADR 0034 的独立实验包新增 GSPO Loss、custom-loss 数值探针、两组
Smoke 控制器与 SwanLab 指标适配器。核心 Loop、Model port、Tool Runtime、
插件和权限系统不变，新增 SDK 只在 `lab` extra 中安装。

参考固定版本 `datawhalechina/agentic-rl-lab@238bc688ca7d5c994c580f732ee9c2c92483bd20`
的 `experiments/gspo/loss.py`、`train.py`。Apache-2.0 许可证保留于实验包
`third_party/agentic-rl-lab-LICENSE`。选择 GSPO 因为已有 GRPO 基线，而且不需要
Critic、Teacher 或 Reward Model。ReTool 的工具闭环与已有能力重合；DAPO 补采、
AgentOPSD Teacher 会增加当前阶段的费用与变量，暂不采用。

## 多轮适配与偏差

每轮仍用实际 Prompt 和右移 Datum，整条任务的全部 Assistant Token 共同计算
几何平均 importance ratio，Observation 不承担直接 Loss。沿用组内居中 Advantage，
不除标准差，以轨迹等权平均，包含零信号轨迹。Smoke 使用 `[0.8,1.2]` 宽裁剪，
另诊断 Lab 默认 `[0.9997,1.0004]`；已有跨引擎 Logprob 偏差可能超过窄窗口。
配置名为 `gspo_multiturn_wide_clip_v1`，不声称复现论文或 Lab 数学实验结果。

SDK 将本地梯度作为 CE 代理权重回传。先实测相同初始状态 single/duplicate
LoRA 更新比值，验证 CE 归一化；两者相对差须≤3%，Prompt 梯度须为零。
首轮补偿测试的 duplicate 更新为 single 的2倍，已阻止训练；因此该 CE 路径
应按 sum 处理、不乘 Datum 数，与旧 PPO sample_mean 区分，保留失败成本并重测。
custom-loss 内含 Forward 和 Forward/Backward，两次均按训练 Token 保守预留。

## 数据与资源

SwanLab 私有项目 `mini-dsh-agent-rl`，只上传白名单数值与封闭配置；关闭自动
终端、Git、运行环境、依赖与硬件采集。原始 Trace、Token、Prompt、私有判据、
参考解、checkpoint URI、凭据留在本地；JSONL 先落盘，远端故障不破坏事务。
历史导入标记来源，不能描述为新训练/新评测。

继续同一累计 ¥50 账本；本轮开始前保守估算 ¥41.163，新增阶段上限 ¥5，包含
数值探针与 8 条工程轨迹。估算不是服务商结算账单。历史测试集已打开，本轮
不用它选择算法/超参，不自动扩大训练。

数值失败、Fresh-policy 活跃裁剪比例>5%、未知 RPC、完整组费用无法准入或
超时均停止，不重放不确定操作。正式比较另冻结留出集，控制 Advantage 与归一化。
停用新入口即可退出；历史源码与数据不覆写，已有归档继续支持离线重放。

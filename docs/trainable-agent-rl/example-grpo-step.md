# 固定反馈示例的一步 GRPO 实验

> **已完成**：1 次真实 optimizer update、8/8 新配对评测；原判据 Base 0/4、RL 0/4。
> 另作不耗模型 Token 的影子判据诊断，双方均 1/4，不替换原分数。新增保守估计
> 2.126 元，累计占用 48.963 元、剩余 1.037 元，无 pending。完整数据与 checkpoint
> 已保存，私有 SwanLab 已回读核对。以下设计及修订按发生顺序保留。

日期：2026-11-08。用户明确设置目标“直接跑后续试验”。本轮执行真实的一次 native
GRPO 更新，再做 8 条新配对评测；不增加累计 50 元上限，也不进入算法收益对照。

## 预算与数据复用

已刷新 JennyMa 的 PyTRIO 账单并切回原 Qoder 页面。11 月 8 日 12–13 时模型 Token
工作空间账单为 0.39 元；上一轮 160 次调用的起止 UTC 均在该已闭合小时，原始
Token、财务 ID 和估价 0.872 元完整对应。用整小时费用加显示余量 0.01 元，即
0.40 元覆盖该 160 项，释放重复估价 0.472 元。这不是项目精确实付或退款。
原 setup 0.05 元、历史余额及不确定预留 2 元保持不变；可用额从 1.843 变为 2.315 元。
另观察到的 11 月 7 日权重存储 0.12 元没有据此释放任何预留。

本轮不重新采样训练数据。复用上一固定 Pilot 首题 path_router_0 的完整示例臂 G4，
Reward=[1,0,0,1]、Advantage=[0.5,−0.5,−0.5,0.5]，包含全部成功与失败，不挑轨迹。
这些动作尚未用于 optimizer update，来自同一个初始零 LoRA 策略。恢复原始含
optimizer 的初始 checkpoint，导出初始 clone 并下载，要求全部 adapter 张量相对
原初始 archive 的 L2 差为零；否则不得用旧 logprob 训练。原 run_id、policy_id、
Token、判据保持不变，通过显式等价证据关联新的运行，而非改写旧记录。

46 个 Datum、4,525 个 assistant Token、227,056 个 padded 位置对应训练上界 1.031 元。
预算固定如下：

| 项目 | 保守上界 CNY |
| --- | ---: |
| 实时元数据/报价额度 | 0.050 |
| 恢复初始 optimizer | 0.010 |
| 保存初始 sampler / 实际下载核对 | 0.100 / 0.020 |
| 一次 forward/backward / 一次 Adam | 1.031 / 0.010 |
| 保存最终 optimizer / sampler | 0.100 / 0.100 |
| 最终实际权重变化审计 | 0.020 |
| 新配对评测 8 条 × 0.09 | 0.720 |
| **完整分配** | **2.161** |

阶段上限 2.20 元，开始前必须容纳完整分配；原来的历史实验与 test 分母均不变。
不能将尚未结算的其他旧账单用于释放资金，也不依赖零信号跳过来满足预算。

## 固定训练和评测

沿用 Qwen3.5-4B、LoRA r16、原初始 seed17、LR=1e-5、clip=0.2、assistant-token mean，
一次 epoch、一次更新，Reward 减组均值、不除 std、不加 KL。mask、右移、后端
sample_mean 补偿及 fresh-policy clipped share ≤5% 门槛复用已验证实现。

评测使用 path_router_1、dependency_batches_1，每题两对 seed，Base/RL 共 8 条。
seed=`836000 + task_index*1000 + repeat*20`，repeat=0,1，逐对交替臂顺序。
两臂均使用相同 tool_example 提示、工具、上下文与全部预算。模型仅权重不同，
Base 是恢复后已验证数值一致的初始 clone，RL 是本次最终 snapshot，不择 checkpoint。
任务是历史项目内已见的开发变体，不是新的全局盲测；一题同家族、另一题为本次
未训练家族，仅用于局部开发迁移诊断。

每条保持 12 步/12 工具、36k 总 Token、3072 总生成、6144 单请求总长、1024 单次
生成和 180 秒 Loop；独立禁网容器、1 CPU、1 GB。正确性由独立测试认定，静态
拒绝和预算终止均保留分母。报告成功数/4、4 对差异、工具/Token/端到端时间与全部
失败消耗。只有一次更新、两个开发任务簇，不作稳定泛化或算法优劣结论。

## 实现、证据和停止条件

全部新驱动位于 `benchmark-results/trainable_agent/example-grpo-step-v1/`，生产 Loop
和原 original-only 训练入口不修改。新增独立、显式的示例 profile 加载器，逐文件核对
来源、完整 G4、提示 hash、policy、seed、判据、采样 start/return 与原始 Token。
加载器先写测试再实现，4 项契约通过；既有计价、账本和 TrainerV2 的 12 项测试通过。

`run_step.py prepare` 固定新协议、8 行队列、归一化 Datum hash 和源码 archive；
`run` 完整准入后才恢复 SDK。初始与最终 archive、独立审计、真实 update journal、
原始 forward 输出、optimizer 回执和含 optimizer 的最终 checkpoint 均保存。每条
评测保留原始轨迹、Token/logprob、候选、独立验证和财务 operation ID/时间。

未知请求、pending、权重不一致、来源/价格变化或费用不足立即停止，不重放未知
backward/optimizer，不增加训练 epoch 或补采样。8 条未运行时也保留声明分母。
所有原始记录留本地，最终仅将数值指标和封闭配置上传私有 SwanLab 并回读。

本文件为执行前设计；结果在运行结束后追加，冻结协议以 `run-01/protocol.json` 为准。

## 已确认停点后的资源修订

首次进程在初始 sampler 保存后、任何 backward/optimizer 之前，因只读审计中的
断言退出。没有记录字段级断言位置，不能断定具体原因；随后只读核验全部归属/
READY 条件满足，实际下载的初始参数与原始 archive L2 差为零。保留失败停点和
同一笔审计预留，核验后结清，不重放保存或训练。

由于原 SDK 进程已结束，继续时再次从同一不可变 optimizer checkpoint 恢复，并
重新导出/下载初始 clone，仍要求实际逐张量一致。新增一次已声明恢复额度：
restore 0.01、initial sampler 0.10、审计 0.02，共 0.13 元；完整上界修订为 **2.291 元**，
阶段上限 **2.30 元**，仍在可用 2.315 元和累计 50 元内。科学参数、G4、8 条评测、
seed、判据及停止门槛完全不变。修订在提交任何训练前写入
`resource-amendment-01.json`，原协议及第一次失败源码不覆盖。

恢复驱动只在确认没有 forward/optimizer 操作、没有 pending 时运行一次。checkpoint
索引未就绪时，只对已知 URI 作有界只读轮询；不自动重试 save、backward 或 optimizer。

第二次也在下载前的断言停下，仍未提交训练；稍后同一 checkpoint 的只读下载成功，
实际参数差仍为零。新增诊断仅记录下载协议/source/状态，不记录带签名 URL；恢复
驱动对 HTTPS 对象下载就绪作有界轮询，不能用 HTTP 或关闭 TLS 校验绕过。
继续所需的一次额外已知恢复/初始审计 0.13 元在训练前记录，完整上界为 **2.421 元**、
阶段上限 **2.45 元**，科学配置不变，见 `resource-amendment-02.json`。

为满足完整准入，另核对了此前 80 条学习研究的 817 次采样：全在已显示的
11 月 7 日 22 时至 11 月 8 日 01 时三段闭合模型账期内。旧估价 4.358 元由
整工作空间费用 3.48 元加三笔显示余量 0.03 元覆盖，校正 0.848 元，所有训练/
保存等非采样估价、历史余额和 2 元预留保留。旧研究的 3,584 个非活动账本输入
hash、4 个策略及 owned run 归属重新核对。此校正属于本轮开始前费用，因此本轮
资金基线从 47.685 校正为 46.837 元，而已发生的 0.31 元消费保持不变，不算训练
节省或退款。可用 2.853 元足以覆盖剩余完整 2.111 元；累计上限仍 50 元。

第三次执行完成了唯一一次 optimizer update 和含 optimizer 的保存，随后最终权重
下载审计在 checkpoint 关联/存储路径断言处停止。错误帧已保留；当时字段值未记录，
不能将具体暂态原因断言为已证实。后续只读确认完整 metadata 后下载已有最终权重，
不再创建训练 client、不重跑训练，仅重连 sampler 完成评测。新增重连保守额度
0.01 元在评测前记录于 `resource-amendment-03.json`，最终完整上界 **2.431 元**、
阶段上限仍 **2.45 元**。原始三次停点、两个额外初始恢复及所有消费均保留。

## 完整实测结果

从首次执行启动至全部评测完成约 **23 分 25 秒**，包含控制面等待和故障恢复，不是
GPU 训练耗时。最终评测进程正常 exit 0；训练 source G4 复用 4 条，新采样训练轨迹
为 0，实际 optimizer update 为 1，新评测轨迹 8/8 完成。没有补采样或择 checkpoint。

### 训练和数值证据

- G4 Reward=[1,0,0,1]，Advantage=[0.5,−0.5,−0.5,0.5]；失败样本也进入训练。
- 原始 46 个 Datum、4,525 个 assistant action Token、227,056 个 padded 位置。
  原始 Token 右移、历史/Observation 零 mask、sample_mean 补偿从记录重建，归一化
  Datum hash 与真实 update journal 一致。
- 原初始权重与实际恢复后 clone 的 L2 差为 0；最终 LoRA 参数差和 B 范数均为
  **0.04249518**。实际 248 个 B 矩阵，r=16、alpha=32、dropout=0；target_modules
  来自实际 archive，见汇总 JSON，不假设只有 q/v。
- 原始 forward 输出重算 fresh-policy clipped share 为 **0.1768%**，低于原 5% 门槛；
  action Token 检查数 4,525。采样/训练引擎仍有概率数值差异，不宣称位级相同。
- journal 为 checkpointed，记录 optimizer URI；optimizer 回执 metrics 为空，不
  填造 grad_norm。实际保存了含 optimizer 的 Train state 和独立 sampler snapshot。
  本轮没有再测“最终 checkpoint 的下一步更新恢复等价”，不冒充新增恢复验证。

训练 checkpoint：`trio://run_5by7wf4rts0t/weights/final_optimizer`；最终 sampler：
`trio://run_5by7wf4rts0t/sampler_weights/final_policy`。未删除这些远端资源。

### 同提示 Base/RL 对照

| 指标 | Base | RL |
| --- | ---: | ---: |
| 原独立判据成功 / 固定声明数 | 0/4 | 0/4 |
| 独立判据已检查 | 4/4 | 4/4 |
| 其中功能测试实际执行 | 2/4 | 1/4 |
| 候选静态拒绝 | 2 | 3 |
| Token 总量，包含失败 | 81,555 | 116,522 |
| 全部 Token / 成功数 | 未定义，无成功 | 未定义，无成功 |
| 平均工具调用 | 7.00 | 10.00 |
| 平均 Loop 步数 | 8.00 | 10.75 |
| 端到端 p50 / p95 | 26.34 / 34.28 秒 | 27.75 / 33.77 秒 |
| 评测采样保守估价 | 0.174 元 | 0.241 元 |

p50/p95 使用 nearest_rank，每臂 n=4，包含准备、Loop、候选采集和独立验证。
四对均双失败，配对差 0，不能据此证明等效；仅一次更新、两个历史已见开发任务簇，
不作稳定泛化或算法优劣推断。RL Token 多约 42.9%，工具调用也更多，没有效率收益。
一条 RL 在实际请求提供商前被 Token 预算保护拒绝，真实采样 start/return 完整，
unknown provider usage=0。该本地拒绝与通用 Trace 未知 usage 占位分开。

### 影子判据：区分静态限制与功能错误

事后分析发现 4 条 dependency_batches_1 候选使用 `set.issubset`，被原静态方法白名单
拒绝，另有 1 条 path_router_1 候选语法错误。为检查是否掩盖正确修复，先冻结单独的
影子协议，再对**全部 8 份原候选**执行 `pure_set_issubset_shadow_v1`：仅在隔离的
判据函数副本中增加 issubset，其他 AST 规则、候选文件和私有测试字节不变。原模块
全局白名单、原 Reward、正式结果和 advance gate 均不修改。

影子诊断在新的禁网容器中顺序运行，1 CPU、256 MB、8 秒，0 新模型/训练调用。
8/8 判据完成，其中 7 份执行了功能测试，语法错误仍静态拒绝。结果如下：

| 后验诊断 | Base | RL |
| --- | ---: | ---: |
| 原结果，保持不变 | 0/4 | 0/4 |
| 影子规则下通过同一功能测试 | 1/4 | 1/4 |

这证明其中两份被原规则拒绝的代码能够通过现有功能测试，原 0/4 不能直接解释为
不带静态约束的纯功能修复率。影子结果也没有显示 RL 差异，不能替换原指标或
用于放行算法对照。另有 1 个影子进程退出 137，保留失败；未单独核验其原因，不
推定为 OOM。所有诊断选择和标签均为事后探索性分析，不冒充预注册实验。

### 费用和数据核验

| 本轮实际保守估价 | CNY |
| --- | ---: |
| 原生 forward/backward | 1.031 |
| Adam | 0.010 |
| 最终 optimizer/sampler 保存 | 0.200 |
| 初始恢复、clone、审计（含两次额外已知恢复） | 0.390 |
| 最终权重审计 | 0.020 |
| 元数据和评测重连 | 0.060 |
| 8 条评测采样，含全部失败 | 0.415 |
| **合计** | **2.126** |

其中额外恢复/重连开销共 0.270 元，没有删掉失败开销。原阶段设计上限经记录修订
至 2.45 元，实际低于最终完整上界 2.431 元。当前累计混合占用 **48.963 元**，
剩余 **1.037 元**，无 pending；仍不是精确项目实付。两次旧估价校正合计 1.320 元
属于之前的费用，不能将本轮净余额变化 0.806 元当作训练成本。

完成审计核对原始训练组、实际权重 archive、journal、forward/optimizer 回执、
8 条 Trace/Token/候选/独立判据、逐调用财务 ID、全部新费用和旧账期校正。
原始测试标签、失败和各次停止日志均保留。当前 486 份受检 JSON/JSONL/文本/CSV/log
无实际 PyTRIO API Key 文本命中，范围仅限该检测对象，不宣称全局安全验收。

### 数据和复现入口

- 可提交的[完整汇总 JSON](../results/example-grpo-step-2026-11-08.json)。
- 本地 `benchmark-results/trainable_agent/example-grpo-step-v1/run-01/` 下
  `protocol.json`、`resource-amendment-01/02/03.json`、`sources.json`、各源码 archive：
  原协议、资源修订和可追溯来源。
- `analysis-01.json`、`trajectories.csv`、`verification-bindings.json`、
  `completion-audit-01.json`：完整指标、逐条结果、判据来源及终态核验。
- `update/`：真实 native PPO forward、概率诊断、optimizer 回执和 checkpointed journal。
- `initial-weight-audit.json`、`resume-*/initial-weight-audit.json`、`final-weight-audit.json`
  及对应 archive：数值相等与实际参数变化的可重算证据。
- `initial-pair.json`、`final-pair.json`、`policy-bindings.json`、
  `evaluation-resume-01/sampling-connections.json`：optimizer/sampler 与真实采样连接映射。
- 各 run 目录保存 Token/logprob、采样 start/return、Trace、candidate、patch snapshots、
  独立验证、财务记录和 artifact manifest；原始数据留本地忽略目录，不提交密钥/Trace。
- `failure-review-01.json`、`shadow-checker-01/protocol.json` 与 `summary.json`：
  后验失败分类及独立版本的影子诊断，不覆盖正式结果。

私有 SwanLab 仅上传正式数值与封闭配置，已回读核对 8/8、1 次更新、Base/RL 各 0/4
和 Token；没有用影子结果覆盖云端原分数：

- [实验汇总](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/re0uvayr)
- [Base](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/iih4923b)
- [RL](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/bmnda9vj)

本轮验证了固定示例 profile 下的真实更新、可保存 checkpoint 与完整评测，**未验证
学习收益**。后续应先为独立新版本明确任务允许的纯方法和判据契约，再规划学习
试验；不能在原分数上事后改规则，也不应据此直接换 RL 算法。本轮不追加付费运行。

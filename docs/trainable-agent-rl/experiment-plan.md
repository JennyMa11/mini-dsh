# Trainable Coding Agent Harness 实验方案

> **当前阶段（2026-11-07）**：[固定执行入口](learning-execution-results.md)已完成离线验收，历史数值组件按原权重和源码复核；新模型结果仍未测。活动账本保守占用42.667元、可用7.333元，完整方案需20.302元，执行未准入。下文各阶段结果按历史时点保留。

> 历史阶段：[小仓库交互诊断](minirepo-results.md)已完成64条；当时累计保守估算48.155元。机制与资源门禁未放行新RL训练。

> 2026-11-07 新增：[Lab GSPO 与 SwanLab 轻量迁移](lab-migration.md)，当前阶段为数值探针和2组工程Smoke；累计仍¥50，新增上限¥5。历史正式结果保持原样。

> **当前结果（2026-11-07）**：40槽训练及192条配对评测已完成，test为Base28/64、RL34/64，成功率区间跨0；格式拒绝下降、总工具调用增加。控制器累计保守占用¥41.163（含¥2预留），最终服务账单待对齐。见[完整结果](../results/trainable-agent-expansion-2026-11-07.md)与[离线复核](reproduction-v2.md)。以下为初始设计及历史规模，当前50元约束优先。

> **历史扩展建议**：原50/100/150档见[扩展实验方案](expansion-plan.md)。本轮按50元路线完成；更高档未启用。历史设计与原始结果保留，不将不同任务集的分数直接比较。

> **2026-11-06历史范围调整**：原[¥50–100轻量方案](lightweight-experiment.md)缩减了初始大规模设计；后来本轮固定累计50元，并按事前[终点评测调整](pilot-budget-amendment.md)执行。下文旧规模仅作历史参考。

原方案冻结日期：2026-11-06；当时为Proposed、仅调研。下文保留历史设计；后续实现、训练与已测收益边界见顶端本轮结果。
入口：[实施路线](implementation-roadmap.md)、[预算](cost-estimation.md)、[数据契约](data-contracts.md)、[来源核验](sources.md)。

## 1. 推荐决策与范围

以现有 Mini-dsh 负责 Coding Task 的多轮执行，以独立实验包负责任务分组、Token 级轨迹、独立评分和训练调度，以 PyTRIO 执行 LoRA 前后向、优化和权重保存。保留原 Agent Loop、插件生命周期、Tool Runtime、Permission 与容器安全边界。

优先路线为 **自建轻量 Python Bug Fix + PyTRIO + Qwen/Qwen3.5-4B + rank 16 LoRA + G=4 + terminal reward**。模型选择依据是已核对 PyTRIO 官方示例及 Agentic RL Lab 使用该模型，而不是假定服务支持所有 Hugging Face 模型。账号中的实际供应、价格、非思考模式、工具模板与导出能力仍是 Smoke 门禁。正式实验前锁定唯一模型和 revision；不将不同基础模型的结果混为 RL 增益。

如该模型不可用或报价超限，先暂停远程训练，评估独立后端的 Qwen2.5-Coder-1.5B-Instruct + 成熟训练库；此路线需要另外验证多轮 rollout 接口，不能把 PyTRIO 当作本地 GPU 训练库。不为降低费用自行实现 GRPO 内核。当前不采用大规模 SWE 任务、大模型、Reward Model、自动课程学习或并行 Agent。

本研究仅能回答冻结小任务分布上的可用性与成本问题。仓库级通用修复能力、真实软件维护收益和训练数据无污染均不能由本实验直接推出。

## 2. 当前代码事实与复用边界

核对 HEAD `a7c5d3bb0eff990e701051e6262a62a82bfbf349`，当前 `experiment` 工作树含既有改动，HEAD 不能代表全部阅读内容。[source-audit.json](source-audit.json) 保存阅读文件 SHA-256；这不是完整冻结或质量门禁结果。

| 真实入口 | 已有能力 | 本阶段处理 |
| --- | --- | --- |
| `src/minidsh/application/agent_loop.py:AgentLoop.run` | Context、Hook、Model、Tool、Observation 循环，显式预算和终态；模型事件含消息、工具和用量 | 原样复用；一次完整 run 就是一条 rollout，不另写多轮 agent 状态机 |
| `ports/protocols.py:Model.complete`、`domain/models.py:ModelRequest/ModelResponse` | 厂商无关消息和工具调用，TokenUsage、错误和推理耗时 | 没有 token_ids、逐 Token logprob、mask、policy_version；由实验 adapter 的旁路记录补齐，不扩大通用返回契约 |
| `composition.py:build_local_runtime`、`RuntimeComponents` | 显式传入 Model、Context、工具后端、Hook 等 | 实验入口使用现有接线；model 是工厂参数，不是 RuntimeComponents 字段 |
| `runtime/plugins.py`、`services.py`、`hooks.py` | 生命周期、服务和扩展接线 | 继续使用；不在 Hook 中做训练或绕过工具执行 |
| `tools/runtime.py:ToolRuntime` | Schema、授权、受控执行、错误和截断 Observation | 复用；无效动作保留轨迹，不能被 adapter 静默修正 |
| `adapters/models.py`、`adapters/mlx.py` | OpenAI-compatible / 本地 MLX 推理；MLX 有解析及格式重试 | 可做既有推理基线；不能直接充当保真训练采样器；实验 adapter 禁止隐藏格式重采样 |
| `adapters/trace.py:JsonlTraceWriter` | 带版本、run/step 的脱敏追加 JSONL | 继续作运行事实；脱敏后的文本不能倒推出原始训练 Token |
| `infrastructure/containers.py:DockerSandbox` | digest 固定、禁网、CPU/内存/PID 限额、容器清理，无宿主挂载 | 原样复用执行环境；每条 rollout 独立容器与工作区 |
| `benchmark/runner.py:BenchmarkTask` | 固定任务/预算/隔离/verification overlay | 复用元数据思路；训练专用 runner 增加 group/policy/split 字段 |
| `benchmark/public_lite.py:verify_candidate` | 在新容器中验证单文件候选及原始测试 | 复用独立验证边界；多文件 patch 验证需新增，不假定单文件函数已支持通用 repo |
| `benchmark/public_swe.py:verify_patch` | 公开仓库 patch 验证路径 | 评估借用其新环境重放模式，不默认等同 SWE-smith/SWE-Gym 数据加载器 |
| `evaluation/harness_metrics.py` | 分母/覆盖率、Token、错误、重复失败、耗时聚合 | 复用；新增 RL 分组统计、训练版本及无变化重复读取诊断 |

现有 README 的历史成功率来自不同模型、任务和冻结版本，不作为本实验 Base 数字。现有 Context/Memory/Verification 能力也不代表经过 RL 验收。实验固定 full history、Memory/Skills/自动修复与额外 guard 关闭；固定原有工具与权限配置。这里“原始 Harness”指冻结的这一组配置，所有模型使用同一配置；不得给 RL 模型额外提示、工具或重试机会。

## 3. 任务集选择与数据治理

| 候选 | 环境/准备成本 | 多轮与验证能力 | 小规模训练适用性及风险 |
| --- | --- | --- | --- |
| 自建 Python Bug Fix | 一个原生架构离线镜像；人工写规格和独立测试是主要成本 | 可设计读取→定位→patch→测试，支持失败回馈；隐藏测试可控 | MVP 首选；需防模板捷径、人工判据缺陷和过于容易 |
| SWE-smith | 官方提供大规模合成修复及环境工具；下载镜像、依赖和挑选有效实例仍有成本 | 真实仓库与可执行测试；更接近 repo repair | 后续外部迁移首选候选；mutation 同源变体必须同 split；公开代码污染未知 |
| SWE-Gym | 官方真实软件任务和执行环境，仓库跨度与依赖较重 | 可验证真实修复、多轮需求强 | 小模型可能全失败，先做少量环境/能力预检；不作为首轮默认训练集 |
| 仓库已有 12 道 public-lite 与内部 benchmark | 已有验证基础 | public-lite 主要为函数修复，多轮需求不足 | 用于离线集成回归，不复用成新实验测试集或据历史结果调 reward |

SWE-smith/SWE-Gym 的定位依据见[官方来源](sources.md)；未下载完整数据、未构建它们的镜像，因此没有本机运行成本或通过率证据。

### 3.1 固定目标规模

主数据集 256 题：train 160 / validation 32 / test 64；另外 8 题工程 smoke 夹具，完全不计科学结论。分 32 个独立小项目家族，每家族 8 题；20/4/8 个家族分别进入三个 split。家族包含共享初始仓库、同源函数、同类 mutation 变体；相似家族合并后再分配，不能随机按行切分。数量不够时先交 MVP，不用参数改名伪造独立样本。

每家族固定 4 易 / 3 中 / 1 难：总计 128/96/32，train 80/60/20、val 16/12/4、test 32/24/8。易：单文件边界条件；中：2–3 文件数据流/接口错误；难：跨模块状态或多条件回归。难度按规格和必要修改面事先标记；真实模型难度分布另报告，不能看测试结果后改标签。

每个工作区 3–8 个小文件、预装依赖、可见失败测试与说明；隐藏测试补充边界和回归。至少一半任务需要定位其他文件或解释可见测试失败。**不把最低工具调用次数设为成功条件**，允许更高效的正确解。以实际轨迹统计是否触发多轮；如果大部分只需一次 patch，则数据集对多轮问题的证据不足。

发布前每题验证：原始 bug 在声明判据上失败、参考修复通过、常见错误修复被拒绝、重复执行结果一致、依赖离线、合法操作不被误拦。测试集 verifier 与 reference 不进入 agent 容器，也不进入训练账户的 Prompt；训练器只接收评分数值及审计引用。

### 3.2 低成本渐进构建

Smoke 使用 8 个专用夹具中的固定任务；MVP 只实现 train 中 16 题与 val 16 题，20 个更新槽按冻结循环顺序抽取。Pilot 扩至 train 64、val 32；Formal 完成 256 题。工程可以分批交付，但家族分配、种子和生成规则先冻结。只用训练子集做模型能力筛选，目标 Base 成功率 10%–70%、混合成败 group 比例至少 20%；达不到就先调任务或模型，产生新协议版本后重跑 Base。不得依据 test 成败删题。

检查 exact hash、规范化 AST、共享模板/源提交、题面及参考补丁近重复；保存 provenance、许可证、生成器版本与去重报告。自建任务降低直接公开题记忆的风险，不能证明预训练无污染。隐藏测试由维护者离线审核，不通过模型自评成功。外部迁移最多另列 16 题 SWE-smith 或 SWE-Gym，仅在另有预算时执行，不纳入默认费用或替代主测试集。

## 4. 集成与训练机制

```text
实验控制器（单进程、顺序调度）
  -> TaskSpec + PolicySnapshot -> 独立工作区
  -> 原 AgentLoop -> RecordingPyTrioModel -> PyTRIO sampler
                  -> 原 ToolRuntime -> Permission -> Docker
  -> Trace + Token sidecar + candidate
  -> 新验证容器 -> RewardRecord -> GroupAdvantage
  -> Datum builder -> PyTRIO forward_backward / optim_step
  -> 新 snapshot / Train checkpoint -> 相同 Harness 评测
```

### 4.1 职责划分

Mini-dsh 实验包负责任务、环境、完整多轮 rollout、工具协议转换、实际 Token 捕获、独立 verifier、Reward/Advantage、mask、实验预算及恢复账本。PyTRIO 负责采样计算、逐 Token 概率接口、LoRA 权重、loss 前后向、优化器状态和 checkpoint 存储。PyTRIO 不自动提供 Mini-dsh 的工具执行、Coding verifier、任务隔离或本项目的 GRPO 分组语义。

用 `import pytrio as trio`，不能与异步库 Trio 混淆。官方示例链路为 `ServiceClient` → `create_lora_training_client(base_model, rank, seed)` → `get_tokenizer()` → `save_weights_and_get_sampling_client()` → `sample` → `Datum` → `forward_backward` → `optim_step`。未来锁 SDK 版本；已读 Lab 提交使用 `pytrio==0.2.9`，只是复现起点，不保证账号服务兼容。[PyTRIO 训练文档](https://docs.pytrio.com/docs/guide/train)

### 4.2 每个训练更新槽

1. 固定当前权重 S_k，选择 B=2 道 train 题，每题 G=4 条独立 run；每条从同一初始仓库、Prompt 和预算开始，采样 seed 不同。
2. 全部 8 条顺序完成后才更新权重；不能在同组中换 snapshot，不能用“替换失败 run”提高成功率。
3. 捕获最终候选，在新容器复位源码后只应用允许的源文件改动，再运行独立判据。拒绝测试、依赖、构建入口、软链接及 verifier 相关路径修改；候选代码本身仍是不可信执行对象。
4. 给所有正常产生的轨迹计算 reward/group advantage；全同 reward 组记零 advantage，跳过其反传，不无限补采样。更新槽仍消耗预算，不能为了凑够更新数延长训练。
5. 将每轮实际请求构造成 Datum。整组一次提交或经验证等价的 micro-batch 累积，仅一次 optim_step。获取 S_(k+1)。最后记录实际非零更新次数与声明槽数。

### 4.3 Token、Logprob 与 Mask：首要验收项

记录的是 adapter 最终渲染后真实提交的 prompt_token_ids、原始 completion_token_ids、每个生成 Token 的 old_logprob、stop/截断原因、模板/tokenizer hash、采样配置及 policy_id。记录解析前文本及对应 ToolCall；解析后的 JSON 重新编码会丢失原始采样概率，禁止当作训练数据。Hook 改写后的请求以 adapter 所见为准。

**默认按模型调用生成 Datum**：第 t 轮令 P_t 是真实输入，C_t 是真实输出，X=P_t+C_t。`input=X[:-1]`、`target=X[1:]`，mask 对 target 位置判断，只对 C_t 的真实采样 Token 为 1。P_t 内的 system/user/tool observation 以及历史 assistant 全为 0；当前生成的工具名称、参数、正文和真实采样 EOS 为 1。后处理附加的 delimiter 不冒充采样 Token。未采样 EOS 不补造。每个新生成 Token 只训练一次。

选择此法是因为 Mini-dsh 会重组消息、工具 JSON 和 Context；Lab 的整轨迹拼接要求每轮 prompt 是上一条完整 token 序列的严格前缀扩展，不能直接假设本项目满足。每轮 Datum 虽重复计算历史前向、费用较高，却能保留采样时真实条件；之后只有字节/Token 前缀证明通过才考虑整轨迹优化，作为独立工程改动验收。[Lab Search-R1 源码](https://github.com/datawhalechina/agentic-rl-lab/blob/167ccf079b7997090d7ac857461efccf87ed6bac/experiments/search-r1/train.py)

非生成位置 `advantages=0`、old_logprob 使用有限占位 0；生成位置填真实 old_logprob 与该 trajectory 的 A_i。无独立 RL `mask` 参数时通过 advantage 表达。必须验证数值稳定性、零位置无直接策略损失、跨轮不会重复计权；工具 Token 仍可作为条件影响后续生成，不能把“mask”误解为移除上下文。[PyTRIO Datum](https://docs.pytrio.com/docs/guide/train)

首版设 temperature=1、top_p=1，避免温度/截断分布与训练 logprob 口径混淆；核验返回值究竟是采样分布还是原始模型分布。少量冻结 snapshot 重算 logprob 对齐误差，阈值先定绝对误差 1e-3，超限调查精度和服务语义，不能直接放宽。重算成本纳入 Smoke。缺失 Token/logprob 的远程错误不造零概率；隔离整组训练输入并保留失败与费用，报告有效训练覆盖率。

### 4.4 初始超参数与目标函数

| 参数 | 推荐值 / 限制 |
| --- | --- |
| 基础模型 | Qwen/Qwen3.5-4B；revision、模板、服务模型身份先核验冻结 |
| LoRA | rank 16；服务支持范围待核；alpha/dropout/target modules 不假定可传，由实际导出配置记录 |
| 学习率 | 1e-5，固定；Adam beta1=.9、beta2=.95，weight_decay=0，grad_clip_norm=1（先验 SDK 支持） |
| 分组 | B=2、G=4，8 rollout/槽；顺序执行，单 Agent/进程 |
| 更新 | 每批单次、单 epoch，无 replay；MVP 20、Pilot 40、Formal 100 槽/臂/seed |
| 长度 | 每次总上下文 8192，单轮输出至多 1024；每 run 生成合计 4096、输入+输出累计 64000 |
| 执行预算 | 12 Loop 步、12 模型提出的工具调用上限、300 秒；全部内部工具也按现有预算计数 |
| Context | full history，上限时结构化终止；首版不用摘要、检索或自动裁剪造成不可见差异 |
| loss | 组相对 advantage + PyTRIO 内置 `ppo`，clip ratio [0.8,1.2]；KL beta=0 |

定义 `A_i = r_i - mean(r_group)`，不做组内标准差归一化，避免低方差成本奖励被放大；记录这一 GRPO 变体，不称为完全复刻原论文。目标是对真实 assistant actions 的 PPO clipped surrogate。官方 GRPO 入门用 `importance_sampling`，它不是带 clip/KL 的完整 GRPO；本方案明确选择内置 `ppo`，并在 Smoke 验证有效 clip 配置，避免只把算法名称写成 GRPO。[损失函数](https://docs.pytrio.com/docs/guide/loss_fn)

首版采用 assistant token 全局均值：sum(mask × clipped surrogate) / sum(mask)。它会让长轨迹贡献更多 Token，必须报告长度漂移。远端 loss reduction、batch mean、自动缩放须实测；如按 Datum 均值，按实际 reduction 做权重补偿，验证拆批前后梯度等价。未确认之前不提交科学实验。将来改成 trajectory mean 是新消融变量，不能在两 reward 臂中使用不同归一化。

不加 reference model 可节省一套前向；代价是不能声称有 KL 正则保护。记录 sampled log-ratio、clip fraction、entropy、output length 与非有限 loss；log-ratio 只是旧策略漂移诊断，不是真实全分布 KL。发生数值异常立即停止；连续 3 槽 clip fraction > .5 暂停调查，不自动网格搜索。

### 4.5 Checkpoint 与恢复

每 5 槽以及阶段结束保存 Train state 与 sampler weights；恢复使用 Train 类型（权重+优化器），仅有 sampler adapter 不足以精确续训。[保存](https://docs.pytrio.com/docs/guide/save)、[恢复](https://docs.pytrio.com/docs/guide/resume)

本地账本另存数据顺序/RNG、slot_id、snapshot_id、已提交 Datum hash、远程操作 ID、预算余额、验证 hash。原子状态机 prepared→submitted→confirmed→checkpointed；optim_step 超时是结果未知，禁止盲重试。优先查询远程状态；无法消歧时终止该执行、从最后已确认 checkpoint 建立新 attempt 并报告重放消耗，不伪称 exactly-once。故障注入测试更新前/后崩溃与恢复；失败组不悄悄遗失。

## 5. Reward 与对照

令 S=1 表示独立隐藏回归与规格判据全部通过，否则 0。安全失败标记并立即停止该批、人工调查，不允许由平均 reward 抵消。基础设施错误与模型失败分别记；评分不完整保持 unknown，但完整任务分母不能删掉。

- R_success = S。无格式奖励、过程 judge、测试通过比例奖励或模型自报奖励。
- R_cost = S × (1 − 0.1 × min(N_tool/12,1))。N_tool 是模型提出的工具动作次数，含无效/拒绝；解析失败的动作尝试另记一次成本机会，规则在 parser 契约中固定。

成功轨迹 reward 为 [.9,1]，失败为 0；成本只在成功后扣，避免 agent 通过提前结束获得高分。该设计是软惩罚而非有理论保证的约束优化；失败组没有成本训练信号是有意的限制。Token 不直接进首轮 reward，避免一次引入两个效率变量；观察它是否随工具次数下降。验证 reward 单调性、零工具失败仍为 0、篡改测试不能拿成功分。不得按输出长度直接奖励“简短”。

| 实验 | 变化 | 假设与解释 |
| --- | --- | --- |
| B0 Base + 冻结原 Harness | 无训练 | 重新测当前模型的基线 |
| R1 RL_success + 同 Harness | 相对 B0 仅权重 | 多轮 terminal RL 是否提高 resolve rate |
| R2 RL_cost + 同 Harness | 相对 R1 仅 reward | 能否在成功率基本不降时减少工具调用 |

R1/R2 从相同 base/LoRA 初始化、同训练任务顺序及 seed 启动，独立产生 on-policy 数据；不能强制共用已经过时的 rollout。模型、预算、sampling、模板、工具、optimizer、验证器和更新槽均相同。Pilot 单训练 seed；Formal 3 个训练 seed（17/29/43）。不把 3 次采样当 3 个独立训练重复。

SFT 不列为必选，若训练子集合法工具调用率低于 80%，先新增 32–64 条仅来自 train 的人工核验示范做冷启动，预算与协议单独登记。此时比较必须扩为 Base、SFT、SFT+RL_success、SFT+RL_cost，并对两个 RL 臂用相同 SFT 起点。Context/工具限制消融本轮不做，避免把 Harness 改善混入策略学习结论。

## 6. Evaluation 与统计

Formal 在 64 个 test 任务上每模型采样 3 次，7 个策略实体（Base + 两 reward 臂各三训练 seed），合计 1344 run。统一 temperature=1/top_p=1，固定任务/重复编号作配对；随机 seed 可提交但远端是否完全可重复单独记录。这里是重复 single-attempt 成功率，不是 best-of-3 或 pass@3。Base 结果只运行一次队列并共享作配对，不伪增样本量。

每训练实体在第 25/50/100 槽做 val32×1，共 576 验证 run。主结果取预声明最终第100槽，val 用于停止和诊断，不按 test 选择 checkpoint。若中止，保留预定行和原因，不用较早最佳 checkpoint 冒充完整 Formal。

| 指标 | 定义、分母与证据 |
| --- | --- |
| task_success_rate / resolve_rate | 独立成功 run / 声明 run；同时列 verified successes / 已验证 run 与 coverage，未运行/unknown 单列；前者是已证明成功比例而非断言所有未知均错 |
| tool_calls | 每 run 提议数、执行完成数分别报告；全队列均值与成功条件均值并列 |
| invalid_tool_call_rate | 未知工具/Schema 无效次数 / 可判定提议次数；解析失败另列 malformed_response_rate，权限拒绝与工具执行错误另列 |
| repeated_failed_action_rate | 复用现有相邻同工具/同参数/同失败类型代理指标；合法复测不算无效 |
| unchanged_repeat_read_rate | 新诊断：同路径/区间/文件内容 hash 不变的重复读取 / 有完整读证据的读取；不是“无效操作”定论，抽查上下文需要 |
| tokens | 每次真实输入+输出，失败消耗保留；cached input 为输入子集；unknown_usage_calls 单列，缺失不能当零 |
| latency | 从准备工作区到独立验证结束的 E2E；另列 Loop、模型、工具、verifier、排队、冷启动；p50/p95 用 nearest-rank，给样本数 |
| 每成功成本 | 全部已花 Token/确认成功数；货币成本同理，含失败；成功数0为 null。推理边际成本和含训练摊销成本分别列 |

保持 `numerator / checked_denominator / declared_opportunities / coverage`，零分母 null。依靠新 sidecar/聚合器补充指标，不改写历史 Trace 和评分器。非法调用率不等于安全拦截率，重复读取不等于无用行为；工具语义选择准确率没有专项标签就未测。

主假设：R1 相对 Base 成功率差；次假设：R2 相对 R1 的效率差。对每个训练 seed 先报配对结果，再以项目家族为 cluster 做 10000 次 bootstrap（固定 seed 20261106），给差值与95%区间；只有8个 test 家族，区间稳定性有限，同时报告逐家族结果。跨 seed 用分层重采样/逐seed区间与范围，不能将同任务、同seed重复当独立样本。二项 McNemar 仅可作为每任务一次固定采样的补充分析，不用于把全部相关 rollout 独立化。

R2 的工程选择标准预定为：resolve rate 差值95%区间下界 ≥ −5 个百分点，且工具数相对下降 ≥10%；达不到为“证据不足/未满足门槛”，不改阈值找显著。64题的试验未必能检出5个百分点效应；Pilot 用观察到的方差估计检验力，预算不足就报告探索性结果。多指标不择优汇报，主要比较固定，其他均标探索性。

失败分类：定位错误、patch错误、回归失败、工具格式/权限、重复失败、上下文/预算、模型服务、容器/验证器、训练对齐。每臂每类最多抽5条按固定seed抽样，追踪原始Trace→Token sidecar→候选→独立判据；至少复核“少调用但失败”“测试被改”“预算超额后修复”三类。报告功能正确性和机制收益分别成立与否。

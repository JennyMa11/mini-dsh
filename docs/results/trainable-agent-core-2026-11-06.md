# Mini-dsh ¥50核心版：多轮Coding Agent RL实测

日期：2026-11-06；分支：`experiment`。结果为一次训练seed的探索性实验，不是正式泛化收益证明。

## 结论与决策

完整闭环已实际运行：Coding Task → 原AgentLoop多轮工具调用 → 原始Token/logprob轨迹 → 独立测试Reward → PyTRIO LoRA/PPO更新 → 同Harness配对评测。研究集40题，主训练80条、配对验证/测试80条，另32条工程校准/Smoke；累计192条尝试均保留。训练20槽中8次非零更新，12组全同Reward跳过。

**没有观察到测试成功率提升。** Base为25/32（78.125%），RL为24/32（75%），RL−Base为−3.125个百分点；按4个test family聚类bootstrap的探索性95%区间为[−18.75，+9.375]个百分点。不能据此证明RL有效，也不能证明RL必然有害。工具次数略少，但单成功任务Token更高，没有证据支持效率收益。

所有付费准备、失败探针、训练和评测共用一个50元账本；保守累计**¥31.38**、无pending，剩余¥18.62。后台工作空间当日已入账模型Token **¥3.37**，当前小时可能继续结算，这个数不是最终逐run账单。不扩大训练、不加入¥100 Reward臂；先修复已定位的编辑/格式问题并改善数据与有效分组率，再考虑新的预注册实验。

## 实际实现与冻结设置

实验包在 [`experiments/trainable_agent/`](../../experiments/trainable_agent/README.md)，独立uv锁、PyTRIO 0.2.9，复用原composition、AgentLoop、ToolRuntime、Permission、DockerSandbox和Trace。生产核心未接入训练SDK；实验Model adapter只交付解析后的模型响应，副作用仍经工具链。训练控制面及隐藏判据不进入Agent工作区。

| 项目 | 本次实际设置 |
| --- | --- |
| 模型 | Qwen/Qwen3.5-4B，服务托管采样与LoRA训练，本机不租GPU |
| LoRA | r=16、alpha=32、dropout=0、bias=none、train_unembed=false |
| 实际target modules | gate_proj、k_proj、q_proj、o_proj、v_proj、down_proj、in_proj_z、out_proj、up_proj；取自导出配置 |
| 优化 | lr=1e-5，Adam beta=(0.9,0.95)、clip norm=1、weight decay=0；每有效组一次backward/optim，PPO clip=[0.8,1.2] |
| Reward/Advantage | 独立判据通过1，否则0；G=4，同题同snapshot；A=r−group_mean，不除std，无reference KL；属于简化GRPO配置 |
| Loss | 每次Model Call形成Datum，保留真实prompt/completion Token及采样q；右移target，仅本轮assistant Token有权重，历史assistant/Observation为零；实测sample_mean再补偿到assistant Token mean |
| 采样 | temperature=1、top_p=1、关闭thinking；组内不更新，无隐藏修复/重采样 |
| 单run预算 | 8步、8次工具、120秒、累计输入+输出24000 Token；请求含输出≤4096、每轮输出≤512、生成合计≤2048 |
| Context/工具 | Full History；Memory、Skills、额外错误恢复、摘要关闭；仅shell argv工具，允许python3/cat/ls |
| 隔离 | 顺序单Agent，独立Linux arm64容器1 CPU/1GB/PID128，network none、drop ALL capabilities、无宿主挂载；新容器独立验证 |
| 镜像 | python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 |

模型输出可能包含原生XML工具块；解析器保留原始生成，非法/截断工具输出返回结构化反馈，不把解析后JSON重编码用于训练。所有费用先持久预留；未知RPC不自动重放。更新journal记录提交、确认、optimizer和checkpoint阶段。只有明确服务拒绝的操作才按调查记录处理，没有把未知训练请求盲目重试。

冻结目录为本地 `benchmark-results/trainable_agent/core-protocol-v2/`：任务manifest、公开fixture与独立判据hash、源码JSON/tar.gz、tokenizer身份、初始policy路径和实际LoRA配置。源码是当时**工作树快照**，不是干净HEAD；原有用户改动未清理。主训练到最终评测逐文件hash校验一致；新增离线分析文件不改变运行配置。

## 任务、分割与变量控制

| split | 家族 | 数量与使用 |
| --- | --- | --- |
| train | clamp、chunks、overlay、moving_sum | 16题；seed17洗牌一轮再重复前4题，20槽×4=80条 |
| validation | runs、intervals | 8题；每策略一次，共16条，只诊断不选checkpoint |
| test | pagination、deduplicate、transpose、group_totals | 16题；每策略两次，共64条，主结果只用此split |
| 工程夹具 | distance、last_even、prefix_sum、defaults | 4题，与研究split分开；24条校准+8条训练Smoke |

每家族4变体（人工预分3易1中），按family分割，避免同源mutation跨split；难度没有独立标定。公开fixture仅4个短文件：solution.py包装入口、implementation.py、一个可见失败复现、README。只有implementation.py可改；判据、参考解和评分标签留在控制面。隐藏检查包括固定边界、异常输入及部分不变性，运行前确认bug失败/reference通过。

这套小函数任务成本低、可自动验证，适合链路MVP；第二文件是包装器，不能宣称复杂跨模块修复能力。未运行SWE-smith/SWE-Gym/SWE-bench，未做训练数据污染检索；公开常见算法及同一家族相关性仍是限制。通过固定检查不等于对所有输入完全正确。

Base采用主训练初始零LoRA sampler，RL采用最后权重；两者使用相同tokenizer/template、Harness、Prompt、预算、任务及每对相同seed。pair内策略顺序交替。没有根据validation挑checkpoint、删失败、补采样或改Reward。两次重复按单次尝试统计，**不是pass@2，也不是32道独立任务**。

## 测试结果与指标口径

| 指标 | Base | RL |
| --- | ---: | ---: |
| 独立成功/声明尝试 | 25/32，78.125% | 24/32，75% |
| 验证覆盖率 | 32/32 | 32/32 |
| 平均Loop步数 | 6.9375 | 6.78125 |
| 工具调用数/平均每run | 182 / 5.6875 | 176 / 5.5 |
| 工具执行错误比例 | 55/182，30.22% | 48/176，27.27% |
| 参数无效/权限拒绝 | 0/182；0/182 | 0/176；0/176 |
| 模型动作格式拒绝 | 21/222次模型调用，9.46% | 22/217，10.14% |
| 相邻重复失败动作代理 | 0/182 | 2/176，1.14% |
| 同argv、同输出cat重复读取代理 | 2/53，3.77% | 1/49，2.04% |
| 输入+输出Token（保留失败） | 287033 | 285811 |
| 全部Token/成功数 | 11481.32 | 11908.79 |
| 端到端p50/p95（秒） | 12.683 / 19.645 | 13.502 / 20.649 |
| 模型调用保守费用 | ¥4.44 | ¥4.34 |
| 模型调用保守费用/成功 | ¥0.1776 | ¥0.1808 |

- 成功以新容器独立判据为准，`finished`不等于成功。全部80条评测均有终态Trace、原始Token和候选hash/验证结果关联；80/80均有多次模型调用。无缺行、unknown_usage_calls=0；未知项未自动填零。
- 工具错误含非零退出、超时或执行失败；Agent主动运行原始失败复现也会计入，不能直接解释为Harness缺陷。参数/权限比例只涵盖已解析Tool Call；模型格式拒绝另列，不能用0参数错误宣称动作格式完美。
- 相邻重复失败采用既有评分器的同工具/同参数/同失败类型代理；cat代理只涵盖成功且未截断的cat调用，Python内读取未检查。正常复测/合理重读也可能被代理计数，不是人工确认的无效行为比例。未对全部动作标注意图，tool_call_accuracy保持未测。
- Token为实际每次请求输入加生成，包含反复携带历史及失败消耗；缓存输入是输入子集。SDK未提供缓存遥测，Trace占位0不能证明缓存命中为0；派生报告把缓存指标改为null/0覆盖率。
- 端到端从run准备到候选独立验证结束，表内分位数使用nearest-rank、每策略n=32。原Trace指标另保留Loop计时及既有线性分位算法，不与表内E2E混用；模型计时包含adapter开销，不是纯GPU时间；验证耗时单独保存。
- 4个test family聚类bootstrap：有放回抽4个家族，保留家族内题/重复，10000次、seed20261106、nearest-rank区间。Base-only成功6对、RL-only成功5对。只有一个training seed及4簇，区间仅作探索，不作显著性或稳定性证明；效率差异未作有足够功效的推断检验。

| test family（每策略8条） | Base成功 | RL成功 |
| --- | ---: | ---: |
| pagination | 7 | 7 |
| deduplicate | 6 | 6 |
| transpose | 4 | 5 |
| group_totals | 8 | 6 |

Validation为Base 5/8、RL 6/8；与test分开描述，不合并以掩盖test差异。没有成本Reward训练臂，因此本次不能回答不同Reward的因果比较。

## 失败案例与结论边界

以下从固定队列取可审计代表，不据此重选测试任务。原始候选/Trace/独立stderr在 `core-eval-01/<run_id>/`：

| run_id | 实际证据 | 说明 |
| --- | --- | --- |
| core-eval-01_014_rl | Loop finished，但区间合并把原区间起点覆盖为lo，隐藏嵌套区间失败 | 模型完成声明不能替代独立检查 |
| core-eval-01_032_base | candidate缩进损坏，AST阶段candidate_policy_rejected | 此标签也涵盖语法错误，不能一概解释为恶意import/越权 |
| core-eval-01_052_base | 转置实现仍使用row[0]，多个模型输出被拒绝，独立值不符 | 未实际修好/编辑执行不足；不是仅看finish状态 |
| core-eval-01_061_rl | 转置range(1,…)仍漏第一列，步骤耗尽 | 更多循环没有自动带来正确修复 |
| core-eval-01_078_rl | totals[str(key)]改变键类型，工具循环failed且独立失败 | 任务接口/数据类型约束仍薄弱 |

80条评测中20条失败：3条在candidate policy/语法阶段拒绝、17条进入容器后判据不通过；其中1条finished但不正确。另12条budget_exhausted却候选通过，仍计任务成功，同时独立保留预算终态。没有用最终成功抹去预算状态。预算约束评分只覆盖8步/24000 Token/8工具/120秒这4项，不代表全部任务约束或安全功能都已验收。

本次信号弱：20组只有8组非零Advantage，更新量小且零信号槽不追加；可能需要更合适的中等难度分布或更稳定编辑行为。该解释是待验证假设，不能从本次数据唯一识别原因。优先离线检查no-op patch、JSON/XML格式和生成截断，下一轮重新冻结独立测试集与参数，禁止拿本轮test反复调参后继续当未见测试。

## 数值、恢复与安全证据

- 零Mask实际LoRA权重差为0；复制/拆分样本权重差比值分别0.99987/0.50655，测定sample_mean并补偿；主训练全部20组从原始Token重建Datum hash一致，Reward、Advantage、组内policy固定和Token对账通过。
- 同sampler重算最大logprob差0.0000616，通过1e-3；跨训练/采样引擎最大0.01575，**未达到原跨引擎1e-3假设**。调查后在主训练前冻结p/q与5%越clip停止门禁，原失败调查保留；不声称引擎位级一致。
- 真正Adam状态保存/恢复后的下一步相对更新差2.7378%，通过预声明3%容差；不能宣称bitwise可复现。
- 最终LoRA已下载到本地 `core-artifacts-01/rl.archive`，64952320 bytes，SHA256 `adec367df2601b97c0d79b9176ffcac6436298188bcbc57252ef6bf68fff0a93`；248个LoRA B张量L2 norm=0.13778455，证明有非零训练权重。初始临时sampler不是可下载checkpoint，未伪造逐张量before/after差值。
- 最终sampler：`trio://run_kkfuzsulr02j/sampler_weights/final_weights`；含optimizer的Train state：`trio://run_kkfuzsulr02j/weights/final_state`。恢复数值经过探针验收；长任务崩溃后全队列自动续跑尚未实现，未知RPC需审计后处理。
- 最终实际Docker安全探针通过：无宿主挂载、network none、drop ALL、host canary隔离且不变；超时/取消后的子容器均被删除。已配置凭据文本检测，受检4288份artifact无命中；范围是此检测器/文件集合，不能宣称所有Secret或全部攻击面无漏洞。

## 费用、时间与资源

| 阶段 | 尝试数 | rollout输入+输出Token | 保守费用 |
| --- | ---: | ---: | ---: |
| 工程S0：24校准+8训练Smoke | 32 | 222071 | ¥6.09（含额外SDK/数值/恢复探针） |
| 核心训练 | 80 | 685858 | ¥14.59（含训练与checkpoint操作预留） |
| validation+test | 80 | 703761 | ¥10.70 |
| 合计 | 192 | 1611690 | **¥31.38** |

Token列仅rollout请求，不包含数值探针backward或文本探针，不能将它当所有计费Token。费用账本还包含这些工程请求；同一累计账本从头保留，没有阶段重置。报价从服务实际model-prices及官方前端计价核验：每百万输入/生成/训练分别¥1.49/¥4.54/¥4.54，忽略缓存折扣；每RPC各类别向上取整到分，训练按padded计算量保守预留，管理操作有额外小额预留。这种逐请求取整比后台聚合计费更保守，不能拿差额推断服务定价异常。

RL表内单成功费用仅为test采样模型费，保留失败消耗；若把本次全部主训练¥14.59摊给24次test成功，则(14.59+4.34)/24=¥0.78875，另不含S0/validation/Base测量开销。该摊销仅示例，不作为部署单位成本。

约22:03北京时间，后台当日三条模型Token账单为¥0.20+¥3.04+¥0.13=¥3.37，均代金券，现金余额¥7.76未变、可用余额¥54.38。排除11月3/4/5历史账单；起始余额观察发生在S0中途，余额差¥2.89不能当全实验成本。账单为工作空间级且当前小时可能未最终结算；当天权重存储尚未独立入账，无法精确归属逐run实付。保留¥18.62余量，不继续付费运行。本报告费用边界截至本次执行；之后长期保留远端checkpoint的存储费另计，未删除用户既有或最终恢复权重。

主训练Trace首尾13:24:30–13:42:42 UTC，跨度18.21分钟；配对评测13:44:48–14:03:25，跨度18.61分钟，包含队列间隙而非纯GPU时间。训练run E2E合计16.57分钟，评测合计18.31分钟；S0工程诊断另耗人工时间，未折算工资。远端GPU型号、显存峰值/秒未提供，不能从Token反推精确GPU成本。本机只承担CPU/Docker和控制面；任务容器限制1GB，未测整机峰值RSS，不能把容器上限当主机峰值。原始证据及探针权重约0.6GB，镜像/Docker缓存另计。

## 复核与复现

主分析为离线派生，不发起模型调用；输出新版本，不覆盖已有报告：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.analyze_run \
  --evaluation benchmark-results/trainable_agent/core-eval-01 \
  --training benchmark-results/trainable_agent/core-train-01 \
  --budget benchmark-results/trainable_agent/core-budget.json \
  --output benchmark-results/trainable_agent/core-analysis-recheck.json
```

当前正式派生文件是 `core-analysis-03.json`，附输入/评分器hash；01保留原始缓存占位口径，02纠正缓存未知，03增加cat重复读取代理，均依据同一批原始运行，**不是三次重新实验**。可分享摘要见[JSON结果](trainable-agent-core-2026-11-06.json)。本地原始Trace、Token和权重不提交Git；缺少本地证据时只能复核摘要，不能声称从公开JSON完全再现模型运行。

摘要生成器为 [`export_summary.py`](../../experiments/trainable_agent/scripts/export_summary.py)，在仓库根目录执行 `uv run python experiments/trainable_agent/scripts/export_summary.py` 可从正式派生文件及本地验收证据再生成同名JSON；它不调用模型。账单观察字段是人工记录的只读UI快照，不能由离线工具重新确认当前后台账单。

完成检查：原项目1072 passed（最终固定工作树复核49.51秒）、实验包55 passed；root ruff format/check、strict mypy（核心175文件、实验43文件）、架构112模块397内部依赖且无违规/环。文档最后修订后再执行对应文档门禁。既有M0–M5全部功能与训练收益分开，不因本次结果追认未触发的旧功能。

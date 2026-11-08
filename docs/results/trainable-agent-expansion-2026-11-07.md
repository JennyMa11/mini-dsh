# Trainable Agent：累计 ¥50 约束内的扩展实验结果

日期：2026-11-07；分支`experiment`。本轮固定40槽训练及独立配对评测已完成，停止新增训练/采样。来源为本机原始轨迹、独立验证和离线评分器；结构化汇总见[结果JSON](trainable-agent-expansion-2026-11-07.json)，重算入口见[离线复核](../trainable-agent-rl/reproduction-v2.md)。

**结论：流程完整可复核；成功率有正向点估计，尚无稳健提升证据。工具调用格式改善，但总工具调用和总Token没有减少。** 当前控制器累计保守占用 **¥41.163，已含¥2不确定预留**，pending为0；它不是最终实付账单。重置卡未使用，100/150元档未启用。

## 1. 当前项目能力与本轮边界

复用原AgentLoop、Tool Runtime、Permission/Sandbox、Context、Session/Trace和Harness评分接口。训练能力位于独立`experiments/trainable_agent/`，主运行时没有引入PyTRIO/Torch依赖，核心AgentLoop未修改。新增的是采样/轨迹与数据转换、可信验证、训练调度、checkpoint和预算桥接；前后向、LoRA优化与PPO loss由PyTRIO实现。

本轮任务为合成Python函数修复，24家族×4变体=96题：train48/validation16/test32，按家族分割。Pilot使用固定seed17洗牌顺序的前40个train任务，每题G4；test的8家族此前没有做模型筛查。数据、参考解和判据由本次编码助手编写，未经过独立人工难度标定；目标难度不等于已测难度，不能据此推断真实仓库/SWE修复能力或预训练无污染。

| 阶段 | 实际执行 | 验收与用途 |
| --- | ---: | --- |
| A任务门禁 | 336项本地容器检查 | 96 bug拒绝、96 reference通过、96固定错误解拒绝、48适用输入修改探针拒绝；组件证据 |
| A开发对照 | 48条 | original14/24、JSON指引16/24；格式拒绝比例未下降30%，按规则保留original |
| A筛查 | 128条 | train55/96、val20/32；23/64个case的两次结果不同；开发准入，不作RL收益结论 |
| A训练Smoke | 8条、2次有效更新 | 权重变化及全链路通过；另一次已知本地预留冲突保留4条额外工程轨迹，没有提交梯度 |
| P主训练 | 160条、40槽 | 28次有效更新、12组零信号跳过；未补采样；训练成功90/160仅作诊断 |
| 配对validation | 64条 | Base/RL各16题×2次；固定最终40槽checkpoint |
| 独立新test | 128条 | Base/RL各32题×2次；同pair seed，策略顺序交替 |

本轮新增Coding rollout合计540条（含额外4条工程轨迹）；另16次小文本计费校准RPC。组件检查不计入模型任务成功率。

## 2. 实际模型与训练配置

- PyTRIO 0.2.9，模型`Qwen/Qwen3.5-4B`，Python 3.12.14控制面；本机无Torch训练/GPU租用。
- LoRA rank16；导出配置核验alpha32、dropout0、bias none，target modules为`gate_proj/k_proj/q_proj/o_proj/v_proj/down_proj/in_proj_z/out_proj/up_proj`；`train_unembed=False`。这些是实际导出值，不代表SDK开放全部PEFT参数。
- lr=1e-5，Adam beta1=.9/beta2=.95、clip norm1、weight decay0；G4、每组一个PPO更新、clip[.8,1.2]，组优势`reward−group_mean`，不除std，不加KL；成功奖励为0/1。
- 每个Model Call单独Datum，使用实际prompt/completion Token IDs与old logprobs；历史和Observation直接loss mask为0，只有当次assistant completion参与。按已测SDK sample_mean做assistant-token mean补偿；不合并不同prefix、重编码或修复非法工具输出。
- 每条最多8步/8工具、24000总Token、2048总输出、单请求4096上下文；temperature1、top_p1、top_k=-1。Docker单任务1CPU/1GB/PID128、禁网、独立工作区，结束后新容器验证。

本次Base是同一训练run在更新前保存的零LoRA：248个B矩阵L2=0；最终B矩阵L2=0.265744，完整adapter差异L2=0.277540。两者使用同一Harness和sampler路径。plain Base开发筛查与本次Base的seed/服务路径不同，不能直接比较其分数来归因训练。

训练采样1,326,134 Token；实际有loss的assistant位置69,123，非零组序列总长961,808，padding计费上界1,739,046。Mask位置不计直接loss仍需要上下文计算，不能用69,123估算全部训练费用。

160/160训练候选有独立测试记录，所有任务/顺序/seed/policy progression、Trace、Reward、Advantage和重建Datum hash一致。28组fresh-policy clip share最大0.468%，低于事前5%guard。采样/训练引擎logprob仍存在近似偏差，早期严格0.001对齐门槛没有通过；本轮保留此限制，不宣称严格同引擎算法复现。

## 3. 独立 test 主结果

每策略64次尝试、32个任务、8个家族；失败与预算终止保留分母和消耗。

| 指标 | Base初始零LoRA | RL最终40槽 |
| --- | ---: | ---: |
| 完整任务成功 | 28/64，43.75% | 34/64，53.125% |
| 总Token（失败保留） | 600249 | 625076 |
| Token/成功 | 21437.46 | 18384.59 |
| 工具调用总数 / 每任务均值 | 326 / 5.09375 | 361 / 5.640625 |
| 格式拒绝 / 已完成模型响应 | 65/423，15.37% | 35/430，8.14% |
| Tool Schema/未知工具错误 | 0/326 | 0/361 |
| 工具执行错误率 | 94/326，28.83% | 111/361，30.75% |
| 相邻重复失败动作代理 | 1/326 | 1/361 |
| 相同成功cat读取代理 | 6/104 | 1/95 |
| 控制器端到端p50 / p95 | 14.80s / 33.19s | 16.11s / 42.30s |
| 测试推理保守估价 | ¥1.509 | ¥1.565 |
| 推理保守费用/成功 | ¥0.05389 | ¥0.04603 |

主成功率差值 **+9.375pp**，family-cluster bootstrap 95%区间 **[−10.94,+26.56]pp**；Base-only8对、RL-only14对。8家族整体重采样10000次、seed20261106、nearest-rank分位数；重复轨迹不当独立任务。区间跨0，不足以确认稳定成功率增益。

探索性效率区间（未作多重比较校正）：

- Token/成功相对变化−14.24%，区间[−44.76%,+48.18%]；总Token反而+4.14%。
- 每任务工具调用增加0.546875，区间[+0.015625,+1.0625]。更多工具动作不能直接判为无效动作。
- 格式拒绝比例差值−7.23pp，区间[−11.06,−3.99]pp；支持本任务分布上的格式可靠性改善，仍受单训练seed限制。
- 端到端平均时长差约+1.60s，区间[−1.00,+4.47]s；本机/网络/服务负载未严格控制，未观察到可靠加速。

Schema有效不代表工具选得正确；执行错误包含修复前有价值的失败测试。cat代理仅覆盖成功且未截断的cat，不覆盖Python读取，也不能判定正常复读是否浪费。语义工具选择准确率保持未测。

任务结果覆盖128/128；私有候选测试覆盖127/128。Base另1条因工作区修改范围检查失败，未运行候选测试，仍记入完整分母。Loop预算四项约束（步数、Token、工具、Loop墙钟）两策略均64/64，不等于全部安全/插件能力验收。unknown provider usage为0，缓存用量未知；不把cached占位0解释为无缓存。

| test家族 | Base成功/8 | RL成功/8 |
| --- | ---: | ---: |
| clock_minutes | 7 | 8 |
| finite_automaton | 2 | 5 |
| integer_text | 2 | 5 |
| query_pairs | 4 | 0 |
| radix_encode | 3 | 5 |
| roman_value | 5 | 6 |
| unescape_text | 1 | 2 |
| version_compare | 4 | 3 |

Validation为10/32→17/32，差值+21.875pp，4家族区间[0,+43.75]pp。test方向更弱，说明只看val点估计不足以扩大训练。本轮在val开始前冻结终点评测优先级：只要40槽完整、审计及资源准入通过，无论val是否提升，都执行完整test；没有选择最佳checkpoint。原60/120槽扩展已推迟。

## 4. 失败与退化案例

以下为本次编码助手对已冻结候选与规格的静态对照，分数仍来自原独立判据；没有新增模型裁判或修改评分。证据均在`final-test-01/<run_id>/`。

- 首个RL-only配对：`integer_text_0` repeat0。Base `000_base/candidate.py`最终`return value`丢弃负号；RL `001_rl`使用`return sign * value`，通过独立验证。
- 首个Base-only配对：`version_compare_2` repeat0。Base `024_base`使用`while b and b[-1]==0`；RL `025_rl`改成`while True and b[-1]==0`，零组件被pop完后访问空列表，独立判据失败。
- 系统退化：`query_pairs`从4/8降到0/8。`085_rl`将append放进`if key not in out`同一行suite，重复键不会追加；`084_base`使用`setdefault(...).append(...)`保留全部值。工具格式正确仍可能产生错误代码。
- `roman_value_3`的`108_base`被控制器记录为`forbidden_workspace_change`，Trace出现写`solution.py`的工具调用，而协议只允许修改`implementation.py`。该条使用workspace allowlist判据记失败，私有候选测试未运行。

后续应先在开发数据审查错误类型和任务质量；本test已打开，不用它调参后再报同一test收益。

## 5. 费用、算力与时间

固定无缓存报价：输入¥1.49/M、输出/训练¥4.54/M，按每请求类别向上取整到¥0.001作影子估价；这不是服务最低计费规则。GPU型号、显存和GPU小时未暴露，不用本机墙钟换算云GPU小时。

| 项目 | 保守占用/估价 |
| --- | ---: |
| 审计结转历史（已含A早先开发/校准1.163） | ¥14.790 |
| 结转后A：筛查+失败工程块+新Smoke | ¥4.761 |
| Pilot训练 | ¥15.134 |
| Validation推理 | ¥1.404 |
| 新test推理 | ¥3.074 |
| 不确定预留 | ¥2.000 |
| 累计 | **¥41.163 / ¥50** |

A全阶段费用为5.924（含结转内1.163），本轮新增完整扩展费用25.536；不能再把A全额与14.79相加。研究摊销示例：25.536/34≈¥0.751/本轮RL测试成功，包含工程与双方评测，不是部署单任务推理费用。训练15.134进一步拆为采样3.436、padding训练上界7.908、管理操作预留3.790；管理预留不是已证实收费项目。

原31.38账本及后续40.38父账本完整保留。结转按完整可核对采样cohort重估，非采样旧预留10.06未释放，所有父hash/operation ID保留；没有清零消费。首次basename冲突额外4条工程样本及0.215占用也没有删除。

当前最终账单尚未对齐；最后成功只读观察为已入账3.87元历史快照，当前小时、存储等未闭合，不能作为本轮最终实付。后续账单UI窗口/host不可用，SDK未发现公开billing接口。费用估算假设供应商遵守冻结报价及max_tokens；长期checkpoint存储费仍需核验。费用范围不含既有编码助手订阅、人工及本机电费。

主训练/val/test的首末artifact时间间隔约63.1/17.1/38.7分钟，合计约119分钟，含服务等待；它是文件UTC时间派生区间。逐run时长来自单调时钟，Loop/model/tool分项与控制器端到端分别保留。本机复用已有uv环境、原生arm64镜像，未租GPU。

## 6. 接下来最小且可控的实验

先做不产生模型费用的账单对齐、开发任务盲审和失败分类；保持本轮test冻结。当前证据不足以支持直接延长到120槽或换大模型。

下一候选为**两Reward臂各20槽×G4=160训练，另全新16题×2次×Base/R0/Rcost=96评测**。共256条，仍用4B/rank16及同Harness。两臂从同一初始Train state/优化器开始，先核验初始adapter tensor一致；同任务顺序、采样参数与预算，只改变Reward。

拟定成本奖励：`r = success × (1 − 0.1 × (tool_calls + format_rejections)/8)`；在当前单动作/步上限内cost有界，失败保持0，避免通过不修复而少调用获奖。系数为下一次待冻结假设，不按本test调优。固定槽数比较，同时报告有效更新数和padding成本；全成功组可能也有梯度，成本臂可能更贵。SFT baseline、Context及安全边界消融暂缓。

按当前轨迹长度估计新增约17–23元，建议**新增上限30元、累计建议75元**：两个训练臂合计24元以内，96条完整评测最坏5.76元，其余用于小型契约校准。长度升至约1.5倍时可能逼近上限，应先用少量工程case重估再准入。任一臂未完成固定20槽就不宣称等剂量Reward对照，全部未运行分母保留。

上述为后续建议，尚未执行或提高当前50元上限。现有`CalibratedBudget`硬绑定50，新档位需要另行实现显式预算契约与保留全部父消费/operation的迁移测试，不能仅提高阶段参数。若实际账单能释放已证实过度预留，优先继续保持累计50；否则单独冻结新累计档后再执行。验收先看独立成功率不下降与动作成本/成功是否改善，再考虑独立training seed及更大任务集；不引入Reward Model。

## 7. 验收与证据入口

- Component：Token/Mask/shift、计费持久化与故障、任务/独立判据及既有真实隔离门禁；任务参考解通过不代表模型成功。
- Trajectory：160条训练的Datum/Reward/policy/seed全量绑定；原始Trace及独立sidecar保留；未知optim不重放。
- End-to-End：完整64条val与128条新test，workspace拒绝保留分母。未触发的Memory/Skills/Context机制及语义工具选择保持未测。
- 已实测：格式整理后从训练时archive回放val和test，两份完整报告与原报告逐字段一致；实验包131项检查通过。主项目最终门禁及版本/hash另存`expansion-v2/quality-gates-01.json`，不以测试数量替代功能覆盖。

[完整执行记录](../trainable-agent-rl/expansion-execution.md) · [终点评测预算调整](../trainable-agent-rl/pilot-budget-amendment.md) · [离线重算](../trainable-agent-rl/reproduction-v2.md) · [原始实施规划](../trainable-agent-rl/implementation-roadmap.md)

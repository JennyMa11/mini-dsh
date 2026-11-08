# 实施路线、接口与验收

> **当前阶段（2026-11-08）**：[交互可学习性 GRPO 已完成](learnability-grpo-results.md)：
> 80/80、3 次有效更新，实际权重变化和私有 SwanLab 回读已核验；配对成功率
> Base 7/16、RL 4/16，学习收益未证实。三对退步轨迹已离线定位。下一阶段先复核
> 数据难度、失败反馈和终止策略，GSPO 收益对照仍未执行；不自动启动新实验。
> 预算占用 47.235 元、可用 2.765 元、无 pending，实付账单待核验。

> **算法比较准入复核**：[复核结论](algorithm-comparison-gate-review.md)确认原正向学习
> 门槛未过，不能只凭整轮完成进入 GSPO 收益对照。现有 native/GSPO 还同时改变长度
> 权重；固定训练数据的离线微分已核对这一差别。候选协议保留，不产生执行许可。

> 历史阶段（2026-11-07）：[固定执行入口](learning-execution-results.md)已完成离线验收；当时新链路初始 LoRA 和学习收益未实测，完整 20.302 元方案未准入。下文各阶段结果按历史时点保留。

> 历史阶段：[小仓库交互诊断](minirepo-results.md)已完成64条；当时累计保守估算48.155元。机制与资源门禁未放行新RL训练。

> 2026-11-07 新增：[Lab GSPO 与 SwanLab 轻量迁移](lab-migration.md)，当前阶段为数值探针和2组工程Smoke；累计仍¥50，新增上限¥5。历史正式结果保持原样。

> **当前结果（2026-11-07）**：40槽训练及192条配对评测已完成，test为Base28/64、RL34/64，成功率区间跨0；格式拒绝下降、总工具调用增加。控制器累计保守占用¥41.163（含¥2预留），最终服务账单待对齐。见[完整结果](../results/trainable-agent-expansion-2026-11-07.md)与[离线复核](reproduction-v2.md)。以下为初始设计及历史规模，当前50元约束优先。

> **历史扩展建议**：原50/100/150档见[扩展实验方案](expansion-plan.md)。本轮按50元路线完成；更高档未启用。历史设计与原始结果保留，不将不同任务集的分数直接比较。

> **2026-11-06历史范围调整**：原[¥50–100轻量方案](lightweight-experiment.md)缩减了初始大规模设计；后来本轮固定累计50元，并按事前[终点评测调整](pilot-budget-amendment.md)执行。下文旧规模仅作历史参考。

原方案冻结日期：2026-11-06；当时为Proposed。下文第1–6节保留原开发清单，实际完成状态以上方结果与执行记录为准。一次只执行当前阶段；所有后续阶段需要前一阶段证据通过。实验设计见 [experiment-plan.md](experiment-plan.md)。

## 当前已落实的模块与下一步接口

| 实际文件（`experiments/trainable_agent/src/minidsh_rl/`） | 输入 → 输出 |
| --- | --- |
| `rollout_v2.py`、`model.py`、`priced_backend.py` | Task+冻结policy+seed → 原AgentLoop轨迹、实际Token/logprob sidecar、候选 |
| `tasks_v2.py`、`verification_v2.py` | 固定任务+候选 → 禁网新容器的独立判据；reference不进入Agent工作区 |
| `contracts.py`、`datum.py` | 每次实际prompt/completion+group advantage → shift后的target/old_logprob/assistant mask |
| `trainer_v2.py`、`train_v2.py`、`transaction.py` | 固定组数据 → PyTRIO一次更新、snapshot、Train state、事务journal |
| `calibrated_budget.py`、`cost_store.py`、`reprice.py` | 父账本/冻结报价/实际用量 → 持久预留、保守估价、下一块准入 |
| `evaluate_v2.py`、`evaluate_final_v2.py` | 匹配initial/final LoRA+固定pair → 64 val/128 test，按预算及完整性停止 |
| `analyze_v2.py`、`trajectory_audit_v2.py`、`secondary_statistics.py`、`replay_frozen.py` | 原始记录 → 来源/hash绑定的审计、聚类区间与无模型调用重算 |

本轮没有工具成本Reward或SFT训练实现。下一候选新增`reward_cost_v1.py`（独立success+动作计数→reward纯函数）、`train_cost_compare.py`（两臂固定20槽的调度）、`configs/reward_cost_20.json`与相应契约测试；先验证失败奖励为0、动作计数有界、初始Train state/optimizer一致，再付费Smoke。现有Model/Tool/Permission/Loop接口继续复用。该阶段为待实施建议，新增预算上限30元、累计建议75元，当前没有启动或提高50元限额。现有`CalibratedBudget`硬绑定50；若以后明确批准新档，还需新增显式预算档契约/父账本结转与防重放测试，不能仅改阶段CLI或另建空账本。

## 1. 范围及架构决策

仓库 AGENTS.md §1 和既有 extension-roadmap 明确不训练模型。本次用户请求新增训练实验规划，必须显式记录范围追加；建议通过 [ADR 0034](../adr/0034-trainable-agent-experiments.md) 把训练放在独立可选实验包，保留核心 Runtime 不训练、不依赖模型 SDK 的边界。当前 ADR 为 Proposed，不表示批准了无限训练或通过了任何阶段。

未来增加 `experiments/trainable_agent/` 独立 uv 项目，Python ≥3.12；自己的锁文件和 PyTRIO/NumPy 依赖，显式引用冻结的 Mini-dsh 源码包。避免让日常 `uv sync` 拉取训练依赖。生产运行不 import 该实验包。初期通过 Python embedding 调用 `build_local_runtime`，不新增主 CLI 命令、不改 `AgentLoop`。如果实际接口不够，先提交最小契约/ADR 变更再改，不能绕过分层规则。

外部副作用仍走明确端口和已有受控基础设施。训练脚本是可信实验控制面，不能让模型看到 PyTRIO Key、checkpoint 写入权限或宿主 Docker socket。新增 artifact 写入与任务创建通过受控 service/ToolRuntime 接口；沿用现有模型 adapter 的网络边界，对训练服务明确 host/port allowlist，模型工具容器仍禁网。不得把训练 RPC 作为模型可调用工具。

## 2. 建议文件与输入输出

下表均为**拟新增**，当前没有这些实现。

| 路径（相对 experiments/trainable_agent） | 责任 / 输入 → 输出 |
| --- | --- |
| `pyproject.toml`、`uv.lock` | 独立依赖与可重现安装；精确锁 SDK/renderer/tokenizer |
| `contracts.py` | 纯 dataclass/enum：TaskSpec、PolicySnapshot、TurnSample、Trajectory、RewardRecord |
| `schemas.py` | Pydantic strict 边界校验；JSON → 版本化契约，禁止未知字段 |
| `model_adapter.py` | 实现既有 Model.complete；ModelRequest + 固定 snapshot → ModelResponse + 原始 Token sidecar |
| `protocol.py` | 工具 schema/模型模板渲染与解析；原始输出 → ToolCall 或 recoverable_error；不执行代码 |
| `recording.py` | run_id/call_index + 实际采样 + Trace引用 → 原子保存、hash；失败即停止该 run |
| `rollout.py` | TaskSpec + snapshot + seed → 原 Loop 的 RunResult、轨迹、候选；顺序单Agent |
| `environment.py` | 创建/复位/清理独立容器；包装现有 Docker/FileBackend/ToolRuntime 能力 |
| `verification.py` | 候选允许路径 diff + 私有判据 → 独立 VerificationRecord；不进入 model context |
| `reward.py` | 独立结果 + 计数 → reward、组 advantage、覆盖率；纯函数 |
| `datum.py` | TurnSample + trajectory advantage → target/mask/logprobs/advantages 对齐数组 |
| `trainer.py` | 固定组数据 + 配置 → 远端一次更新、snapshot 与事务账本 |
| `checkpoint.py` | 远端 Train state + 本地 RNG/任务顺序/预算 → 可审计恢复状态 |
| `evaluation.py` | 原 score_harness + 绑定证据 → 配对统计、CSV/JSON/Markdown |
| `budget.py` | 用量/报价/已提交上限 → 是否准入下一调用/组；预留在途额度 |
| `configs/{smoke,mvp,pilot,formal}.toml` | 预声明协议及 immutable hash，不存 Key |
| `tests/test_*.py` | 契约、adapter、故障恢复、容器安全；unit 不联网/不跑真实shell |

另拟新增 `benchmarks/trainable_python/` 的生成器、公开 fixture 与 manifest；隐藏 verification/reference 存在 agent 不可挂载的控制面目录，评测时临时注入新容器。即使版本管理在同仓库，也不能把整个仓库复制给 agent。拟新增 `scripts/trainable_agent_preflight.py` 只检查，不默认付费调用。

现有代码修改应限于明确缺口：若需要共享候选验证函数，先增加契约测试再提取到既有 benchmark/infrastructure 合适层；不要让 application import experiments。未来扩展结构检查器和 CI 以覆盖实验包，保持生产四项门禁及架构检查。当前任务只写文档，不执行上述变更。

## 3. 分阶段执行

### S0：Smoke，先离线后最小远程调用

预估开发 3–5 人日；不是训练墙钟。

- 冻结完整工作树快照、dirty diff、源码/依赖/镜像/模板 hash；当前 source-audit 只是阅读清单。
- 核验 PyTRIO 模型清单、精确 SDK 签名、rank 支持、Token/logprob、费用单位、Train恢复与 adapter导出；保存官方报价页面/账号报价时间戳。不安装未经锁定的“最新版”到主环境。
- 构建8个工程夹具，至少涵盖成功、失败、非法工具、工具超时、预算截断、测试篡改、容器取消和零方差组。
- 先写 Recording Model / Datum / Reward 契约测试，使用确定性假采样；再接现有 Loop。冻结 parser，不静默补工具参数或重采样。
- 远程先做16条校准 rollout；再2槽×2题×4条=16条训练 rollout；最多32条，不扩大到完整任务集。
- 验证更新后 policy hash 变化、冻结 snapshot 重采样身份、Train checkpoint 恢复、mask零位置及真实工具调用；成功数量不用于宣称模型变好。

验收：下面覆盖矩阵全部有证据；首轮成本/耗时测量报告完整；没有 Secret、未授权副作用、孤儿进程；一旦 logprob/mask/reward 错误立即停止。付费部分的模型/价格无法确认则只交离线结果，远程项保持未测。

### S1：MVP，完整闭环

预估再3–5人日。实现 train16/val16，R_success 20个更新槽、160条训练；Base与末checkpoint各val16×2=64条评测。总224条。记录全链路 artifacts，完成32次评分配对的正确关联，至少有实际非零advantage更新；零信号不补采样。验收是“流程可复现且能正确停止/恢复”，不以验证集提升作为工程验收条件。

若前10槽全零优势、基础合法调用率<80%、服务未知用量持续出现或费用超上限，停在MVP调查。可改模型或冷启动SFT，但需新协议，不沿用原实验数字。

### S2：Pilot，是否值得扩大

预估再2–4人日；train64/val32，两reward臂各40槽、一个训练seed。训练640条，Base/R1/R2各val32×2=192条；共832条。

晋级条件：安全/对齐全部通过；混合reward组比例≥20%；验证集R1对Base点估计至少+5个百分点，或R2在成功率点估计下降≤5个百分点时工具调用下降≥10%；同时预计Formal落在资源上限内。此处只作探索性决策，不能称显著收益。无信号仍可形成负结果，停止扩张；如用户另行选择研究负结果，按新的预算记录执行。

### S3：Formal，冻结后执行

预估数据/审查再5–10人日；完成train160/val32/test64。两臂×3训练seed×100槽=4800条训练；验证576条、最终测试1344条；共6720条。每个臂从相同起点独立训练，不把Pilot权重当Formal初始化。

先登记协议hash和完整队列，再运行；训练/评测配置、失败定义、统计方法和主次假设冻结。输出完整分母、各seed差值/置信区间、失败案例与安全独立结论。预算先到即止，未执行行留在报告，不能宣称完成了全Formal。不增加额外checkpoint测试、SFT/Context消融或外部任务来追逐阳性结果。

## 4. 必测覆盖矩阵（初始全部未测）

| case_id | 触发 / 预期 | 禁止行为 | 检查与证据（拟定） |
| --- | --- | --- | --- |
| rl_mask_01 | 两轮工具交互，只有新assistant token有mask | observation或历史assistant参与直接loss | golden token数组、梯度比较，`checks/mask.json` |
| rl_alignment_02 | 中文/空白/转义JSON/EOS/截断 | decode再encode替代原token | 原始token+logprob长度/offset断言 |
| rl_context_03 | Hook改写、历史重排 | 错把不同prefix强拼 | 实际请求hash及每轮Datum回放 |
| rl_reward_04 | 原bug、正确patch、测试篡改 | 只凭finished/可见测试评分 | 新容器独立结果、candidate hash |
| rl_group_05 | 全成功/全失败/混合/服务中断 | 分母删失败、无限补采样 | group清单、advantage及有效覆盖 |
| rl_policy_06 | 同组多轮 | 中途替换权重 | 每turn policy_id一致 |
| rl_reduce_07 | 同批整批与拆批 | reduction改变权重 | 数值loss/梯度一致性；阈值运行前冻结 |
| rl_resume_08 | 更新前后进程崩溃、RPC超时 | 双重optim_step、假装恢复 | 远端操作ID、本地事务账本 |
| rl_sandbox_09 | 超时、取消、路径逃逸、禁网探针 | 宿主副作用/遗留进程 | 真实文件/进程/网络检查 |
| rl_secret_10 | 测试凭据canary | Token sidecar/Trace/模型输入泄漏 | 输入输出扫描及权限检查 |
| rl_budget_11 | 达Token/时间/费用上限 | 后续调用、不计失败消耗 | 准入拒绝事件、实际账单 |
| rl_eval_12 | 漏行、未知用量、零成功 | null转0、删未运行行 | 冻结清单与聚合fixture |

每项保存判据版本、run_id、期望/禁止行为、实际结果、证据hash及审核者来源；无证据为未测。模拟通过不能代替真实容器安全检查。质量门禁：根项目 `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy src tests`、`uv run pytest`、架构脚本；实验包另有等效严格类型及分层测试。每次跨边界实现先加契约测试，Bug修复带回归。

## 5. 风险与替代方案

| 风险 | 检测 | 替代/成本影响 |
| --- | --- | --- |
| 服务不支持模型/LoRA配置/精确token | S0能力核验 | 优先换同服务已支持小模型并重冻基线；本地成熟库是独立路线，增加适配成本 |
| 多轮前缀不一致 | prefix/hash对比 | 每调用Datum；增加前向Token，先保正确性 |
| sparse reward或工具格式不熟 | train校准、混合组比例 | 优先训练数据难度调整，必要时独立SFT；不奖励无意义工具动作 |
| Reward hacking | 私有回归、错误解探针 | 限候选路径、全新验证容器；暂停而非调高奖励掩盖 |
| 学到提前终止/少读文件 | 成功率与工具数联合分析 | 成功门控成本惩罚、R1/R2分开；不只看平均长度 |
| 服务降级/价格变化 | 锁报价及实际用量 | 费用准入停止；不自动购买更多资源 |
| 本地架构与镜像不匹配 | 原生平台预检 | 优先纯Python原生镜像；SWE外部任务另议，禁宿主降级隔离 |
| 隐藏判据被候选代码窥探 | 私有目录/进程与攻击测试 | 不可将独立容器等同完全防作弊；限制修改面、审查恶意候选，必要时隔离验证驱动与被测进程 |
| 统计功效不足 | Pilot方差/家族分析 | 承认区间宽；不在看到test后增样本或换主指标 |

## 6. 现在的第一批开发任务

下一次仅启动S0：①冻结工作树；②确认PyTRIO账号能力和报价；③实现8题工程夹具及离线验收矩阵；④写Token/Mask/Reward契约测试；⑤接最小Recording adapter；⑥最多32条远程rollout与一次恢复演练；⑦生成S0报告后决定是否进入MVP。本次尚未执行这些任务。

# Extension v1：M5 总验收与停止结果

2026-10-30。状态：**按预声明预算停止并完成报告封版；功能验收通过，真实模型配对收益不可评估。** 本轮没有补跑、替换失败结果或追加预算。Skills、Verification、Memory 继续默认关闭。

本报告依据[主计划 v1](../extension-roadmap.md)的完成定义及总预算停止条款收尾；不表示完成了全部 90 次实测，也不表示满足 Token 约束。[机器报告](extension-m5-2026-10-30.json)保留完整分母与未知项。

## 源码、批准与固定队列

实际 Actor、评分器及功能预检源码 SHA-256：`e7232accf73225fdbb0637aecc3736b344d00e58d5f49d80b0428dc024baf1e5`，805 个受控输入。实测快照 manifest SHA-256：`06d7b56a914f5fb59b12ebf28d110dbd5af2745ba4289d0dbb4790796605f724`。

你在本次会话明确“确认配置并批准这3条固定种子”后，维护工具按 validate → approve → commit 处理独立副本中的三个 revision-1 条目，操作身份为 session_user，UTC 时间为 2026-10-30T01:12:30.292865+00:00。两条教学证据与一条人工配置保持原内容；原审阅文件和数据库没有被覆盖。[授权记录](../../benchmark-results/extension-v1/m5-authorized-20261030/seed-approval-v1/authorization.json)及[种子清单](../../benchmark-results/extension-v1/m5-authorized-20261030/seed-approval-v1/seed.json)绑定了来源和 hash。

只运行一轮固定队列：6 题 × 3 次重复 × 5 组，组内顺序种子 20261029。每题名义 16 步、32 工具调用、180 秒、8,000 Token，单次最大输出 1,024 Token；总名义 720,000 Token。固定 endpoint 的 DeepSeek 请求 model ID 为 **deepseek-flash**，temperature=0、thinking=false；333 次响应均报告 deepseek-flash 和同一 system_fingerprint `bc19ba97cbe016479903222bb1578db6`。这记录服务端身份信息，不证明远端权重不可变。

实测后仅更新 README、评测说明、Memory 说明三个文件的状态与导航。交付源码身份为 `52d91f90f674ad15cf7f8e5d4def869d35c6bdecb29bd881ed579f49406909d9`；运行时、评分器、测试、任务和冻结判据逐字节不变，见[文档差异证明](../../benchmark-results/extension-v1/m5-authorized-20261030/documentation-delta.json)。本报告及 JSON 是单独索引的生成产物，不进入源码自引用 hash。

## 实测结果及约束

已执行并独立验收 **80/90** 条，覆盖率 **88.89%**；剩余 10 条均标记 not_run，仍占完整声明机会。产物独立判据通过 **6/80（7.5%）**；完整 90 条任务成功率为未知，不把未运行填成成功或失败。80 个 Loop 全部以 budget_exhausted 结束，因此**约束内任务成功为 0，已检查运行的全部预算约束满足率为 0/80**。产物正确与预算满足分别报告。

| 组 | 已验收 / 声明 | 产物通过 / 已验收 | 已知 Token |
| --- | --- | --- | --- |
| Baseline | 16/18 | 4/16 | 144,012 |
| A：纯审计 Hook | 16/18 | 2/16 | 143,528 |
| B：加 Skills | 16/18 | 0/16 | 145,060 |
| C：加 Verification | 16/18 | 0/16 | 145,581 |
| D：加已批准 Memory | 16/18 | 0/16 | 143,177 |

这是不完整队列的描述性计数，不能据此归因机制优劣。缺失的是 project_constraint 的后两次重复，共 10 条；其他五题各检查 15 条，该题检查 5 条。

实际消耗 **721,358 Token**（输入 696,276、输出 25,082），比名义总预算多 **1,358**；全部失败消耗保留。缓存输入 600,060 是输入子集，不重复相加。333 次 adapter 调用的用量均已知，没有格式重试或自动重试；未知用量调用为 0。80 条已尝试任务的实际消耗完整已知；完整 90 条预测消耗保持 null。旧 Trace 中 cost_usd=0 是未测占位，本报告不计算费用。

Token 在响应后才能确定，所有 80 条均有在途超量。前 79 条获 8,000 Token，最后一条按总预算剩余额度获 7,202；这项实际资源差异明确保留，不能视为完全相同预算的完整配对。审计确认到达已知单题或总预算后没有启动下一次模型/工具调用，整轮停止规则遵守；**这不等于实际 Token 预算满足**。

观察到的平均 Loop 步数为 4.1625，平均工具调用数为 6.6（均 n=80）。Loop 延迟 P50=4,537.24 ms、P95=6,308.35 ms（n=80，冻结评分器按 `(n-1)*p` 线性插值）。计时从 Loop 内部起点到 RunResult 构造，不包含组装、结束清理、产物捕获和独立验证，不能当作用户端完整耗时。

## 机制收益与失败归属

预声明五个对照 A−Baseline、B−A、C−B、D−C、D−Baseline 的成功率、Token/成功、Loop 延迟差值及 95% 区间**全部为 null**：每组缺少 2/18 个配对机会，不筛选完整子集计算收益。结论为 **not_assessable**，不能声称“显著无效”或“有收益”。固定的六题聚类 bootstrap 口径、10,000 次重采样和随机种子不变；本轮未执行缺证据的重采样。

原 CLI 将 real_model_results 限定为 execution.status=completed，导致已停止的真实队列也输出 false。[原派生结果](../../benchmark-results/extension-v1/m5-authorized-20261030/derived/comparisons.json)原样保留；[独立派生结果](../../benchmark-results/extension-v1/m5-authorized-20261030/final/comparisons.json)依据真实 provider/调用记录将该元数据更正为 true，移除错误的 not_real_model_results 阻塞理由。冻结算法、全部组统计、差值、区间和收益结论均逐项相同；仍被 incomplete_declared_cohort 阻塞。生产评分器未事后改写。

528 条工具 Observation 中，有 **78 次 Git 非零退出 127**：冻结镜像没有 Git 可执行文件，这是已记录的工具可用性/环境限制。另有 **73 次后端命令策略拒绝**，与外层 PermissionSystem 拒绝分别统计。通用评分器的 151/528 工具错误计数混合这两类，不应解释为 151 次已执行进程失败；外层 permission_denied=0 也不代表没有命令策略拦截。本轮没有工具选择准确率、允许动作误拦率或危险动作拦截率专项标签，不能从这些计数推断它们。

仅 6 个工作区发生内容变化（忽略 Python 缓存），其余 74 个在修改前耗尽预算。真实队列中 runtime_verification_completed 为 **0 次**，因此 C/D 的完成验证机制未在实测中触发；它的功能契约通过来自单独 V01–V05 验收。D 组 Memory 选择匹配和实际模型请求暴露均为 **16/16**，声明机会 18，覆盖率 88.89%；选择或暴露不等于语义帮助。16 个 D 组数据库各保留原三个 committed 条目，评测期间新增 committed 条目为 0。

## 功能、安全与工程验收

当前运行源码的 **28/28 功能矩阵项通过**，覆盖 Hook、Context、Skills、Verification、Memory 和 E01–E03；定向契约 557 项保留 175 条真实 Loop Trace。七个原生安全/契约驱动及五个离线示例均通过。当前源码与独立解压的冻结源码分别通过四项门禁；实测后文档状态更新的工作树再次通过 ruff format、ruff check、mypy strict 和 **990 项 pytest**，类型检查 163 文件。测试数不替代功能覆盖矩阵，见[完整覆盖与机会表](../../benchmark-results/extension-v1/m5-authorized-20261030/final/coverage.json)及[准备期证据](extension-m5-preparation-2026-10-30.md)。准备期的待批准、0/90 状态属于历史记录。

80 个 Actor 与 80 个独立验收容器均使用原生 linux/arm64、1 CPU、1 GiB、128 PID、无宿主挂载、禁网、丢弃全部 capabilities 及 no-new-privileges。Actor 移除后才运行独立判据；所有 **160 个容器已移除**，捕获 tar 没有在宿主解压。原生探针另验了越界、网络、危险命令、合法对照、超时/取消清理等必测项。不能用本轮容器计数替代其他平台的 OS 隔离验收。

精确检测本次选用 API 凭据在 1,842 个实测证据文件、运行日志及 805 个受控源码文件中出现次数为 0；凭据未被打印、写入报告或 hash。此结果仅适用于该精确检测对象，不推导所有 Secret 零泄漏或所有政策零违规。更广语义 Memory 效用、跨主题冲突、宿主同 UID 恶意进程和断电恢复不在已验证保证中。

## 封版边界与复核

M0、PR 1–4 的历史封存保持原样；M5 完成的是既定功能验收与这一次预算停止实验的完整报告。没有删减 90 条声明机会，也没有把未触发机制补认成实测。本轮不新增功能、修改镜像或策略后追补成绩。后续改进 Git 可用性、命令可发现性或 Token 效率须作为新范围，重跑须另行明确版本、原因与总预算。

证据根目录：`benchmark-results/extension-v1/m5-authorized-20261030/`。原始逐运行 Trace、有效配置、独立判据、产物 hash 和容器回执在 real/runs；总账在 real/execution.json。只读复核命令：

```bash
uv run python benchmark-results/extension-v1/m5-authorized-20261030/verify_final.py --current
```

该命令核对封存、源码、文档差异及派生报告，不调用模型、不启动评测、不读取凭据。完整定义核对见[完成审计](../../benchmark-results/extension-v1/m5-authorized-20261030/final/completion-audit.json)。工作区已有改动均保留，本轮没有执行提交或推送。

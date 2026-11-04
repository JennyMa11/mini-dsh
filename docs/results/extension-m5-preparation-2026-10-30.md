# Extension v1：M5 准备验收与待批准项

2026-10-30。**M5 离线准备已验证，完整目标尚未完成。真实模型队列 0/90，等待三个实际种子的明确人工批准。** PR 4 已接受，见 [阶段报告](extension-pr4-2026-10-30.md)。

当前分析源码 SHA-256：`e7232accf73225fdbb0637aecc3736b344d00e58d5f49d80b0428dc024baf1e5`。受控源码归档固定 805 个输入，证据根目录 `benchmark-results/extension-v1/m5-preparation-e7232accf73225fd/`。没有提交、推送、修改原 Trace 或启动真实模型请求。

- 当前源码与独立解压归档均通过四门禁，**990 项测试、163 文件严格类型检查**全过。
- 定向契约 **557 项**全过，保留 **175 条 Loop Trace**；M5 当前源码的 28 行功能预检通过，七个原生安全/契约驱动和五个可运行示例均通过。它们不替代真实模型任务结果。
- 新增配对统计契约 13 项、CLI 诊断 2 项；公开 report 保留完整声明分母、输入与评分器 hash，配置及 execution.json 也绑定原始 bytes hash。

分析细节已在真实数据产生前固定，见 [分析口径](../extension-evaluation.md)：六个 case 聚类、保留各三次重复、种子 20261029、10,000 次配对重采样、95% percentile 线性插值区间。报告成功率、包含失败消耗的 Token/成功和记录的 Loop 延迟差值；未定义的零成功副本不会被丢弃。缺失、未知用量与未闭合 Trace 保持未知，不挑选完整子集替代 90 行。

对 PR 4 已封存的 **90 行脚本演练**进行了派生补算，原 Actor 源码为 `7249c0eae026ca0626883329d347cbaaceb01f0c408344d17a5b36a09765250b`；本次评分器来自当前 e723 源码。原始 Trace、冻结判据与源码包保持不变。五个配对差值及区间、分组统计、Memory 选择匹配与模型实际暴露计数均可生成；所有收益标签为 not_assessable，不能把脚本数据解释成 DeepSeek 收益。

本地派生报告见 `historical-derived/report.md` 和 `comparisons.json`。Context 的 Memory 选择器匹配来自 metadata，暴露来自实际模型请求中的低信任 User 段；未匹配/未观察机会不从声明分母删除，选择不等于语义帮助。延迟范围是原 Trace 中 Loop 计时起点到 RunResult 构造，不含组装、结束清理、捕获和独立验证。

前置门禁已实测：未批准冻结种子的 M5 包拒绝 `run --provider deepseek`，返回 approved_frozen_seeds_required，未创建执行目录，未提供凭据、未调用服务。三个实际候选仍为两条 validated、一条 proposed；原数据库和审阅 JSON 保持不变，批准/提交均为零。原审阅 JSON SHA-256 为 `fe38a84ea6b72b5acf485172e1321a04367080ad457c0afab509a771f9299e17`。

仍需完成：

1. 明确确认配置并批准 [三个已审阅候选](../../benchmark-results/extension-v1/pr4-teaching-development/attempt_2/review.md)，在新副本中受控批准/提交并冻结。
2. 将实际种子 hash 绑定本次最终源码及当前功能/安全证据，然后按原顺序运行唯一一轮真实 90 行队列，名义总预算 720,000 Token。
3. 保留停止、失败、未知用量与未运行机会，产出真实数据比较、资源/安全审计与最终 M5 完成结论。未看到明确收益也如实报告，不自动扩题或重跑。

[主计划第 275 行](../extension-roadmap.md#7-对照实验协议验证功能与评估收益分别做)明确要求“先验证并批准、冻结 hash”。该明确批准尚未收到，自动续行不能代替人工授权。同一条件已在 seed-prep 947、功能证据 960、PR 4 release 975 和本次准备阶段持续存在；blocker-history.json 绑定先前核验记录。现在没有可绕过该输入的剩余离线实施步骤。

证据由 audit_preparation.py、source_manifest.json、evidence_index.json 和 preparation_seal.json 绑定；准备封存状态不是 M5 完成。PR 4 历史封存已再次核验，保持原样。

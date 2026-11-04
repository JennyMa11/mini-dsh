# Extension v1：M5 配对报告与执行门禁

本轮范围沿用 [扩展主计划 v1](extension-roadmap.md)，PR 4 功能接受见 [阶段报告](results/extension-pr4-2026-10-30.md)，本轮实测与停止结论见 [M5 最终报告](results/extension-m5-2026-10-30.md)。本文的分析细节在真实数据产生之前固定；当时种子尚待批准，该准备状态已保留在历史源码包中。本轮没有改动 M0 任务、队列、预算、模型参数、成功判据或本文统计方法。

## 固定分析口径

`evaluation/extension_comparison.py` 提供纯函数 `compare_extension_groups` 和带类型的 ComparisonRun / ComparisonReport；不读盘、不调用模型、不修改 Trace。统一 `scripts/extension_acceptance.py report` 从原始 Trace 和独立 sidecar 提取各声明槽位，再调用该函数。

- 声明分母仍为六个 case × 三次重复 × 五组，共 90 行；每组 18 行。按 case_id/repeat/group 配对，拒绝重复或额外 case，不挑选完整子集替代预声明集合。
- case_id 按字典序固定，随机种子 20261029；每次从六个 case 有放回抽取六个，保持同 case 的三次重复及两组配对，重采样 10,000 次。
- 95% percentile 区间使用排序后 `(n-1)*p` 的线性插值，端点 p=.025/.975。六个已知小型任务的区间仅用于探索；区间不作多重比较校正，不支持总体泛化或成功率非劣结论。
- 差值统一为增强组减前一组：A−Baseline、B−A、C−B、D−C。另报告 D−Baseline 组合效果，不归因到某个单一模块。

成功率依据独立 task_success，`finished` 不代表成功。分组成功率同时保留分子、已检查分母、18 个声明机会和覆盖率；任何配对槽位未知时，完整配对成功率差值及区间保持 null。

Token/成功使用该组所有运行的已知完整 Token 总和除以独立成功数，保留失败、取消、预算终止的消耗；缓存输入不重复加到总 Token。若任一声明槽位用量不完整，完整总量与 Token/成功为 null，数字下界另存 known_tokens。成功数为零时比值为 null。重采样中出现零成功的副本会计入 undefined_resamples；只要存在未定义副本，区间为 null，不删除这些副本后计算“更好看”的区间。

延迟使用原始 `run_finished.duration_ms`：从 Loop 计时起点到 RunResult 构造，包含模型/工具等待和该范围内的编排；不包含组装、finishing/end 清理、产物捕获或独立验证。报告组均值、样本数、差值和区间；模型服务延迟、验证耗时与 H05 的纯 Hook 开销保持各自口径。

原有 Harness 指标另给每组 observed-trace 描述性计数及缓存统计。该视图只覆盖已观测 Trace；完整声明队列的量和未知项以 paired_comparison.groups 为准。Token/成功另存分子、成功分母、联合已检查运行数、声明机会和覆盖率。

## Memory 机会与模型暴露

D 组有 18 个声明机会，分别报告两个带分子/分母/覆盖率的计数：

- memory_selector_match：至少一次受控 Context 组装记录 memory_match_count > 0；无检索证据保持未知，不记成零。
- memory_model_exposure：实际 model_call_started 的 User 消息中出现 project_memory；没有模型请求时不把未观察机会算作已验证暴露，已请求但没有该段则记为 false。

选择器匹配不代表经验在语义上有帮助，也不能替代任务成功或单变量效果。全部 18 个 D 槽位仍进入 D−C，不因未匹配而删样本。

## 收益判定的证据条件

只有完整真实模型队列、全 90 行结果/用量完整，且提供同源码同冻结种子的有效 PR 4 安全证据封存，才启用收益标签。字符串形式的“true”不当作布尔证据。

- success_signal：成功率差值区间下界 > 0。
- efficiency_signal：成功率差值点估计不下降，且 Token/成功差值区间上界 < 0；仅称效率迹象，不称成功率非劣。
- 条件不满足时保持 not_assessable；证据完整但没有达到收益阈值时为 no_clear_benefit，即“未观察到明确收益”。

脚本演练即使所有任务通过、区间为正或为负，也不生成真实模型收益结论。Safety receipt 只复用已签署的可执行安全证据；report 不自行签署阶段接受或自动批准种子。

## 命令、产物与历史补算

默认离线入口可在全新目录复现：

```bash
uv run python scripts/extension_acceptance.py freeze --output benchmark-results/extension-v1/my-snapshot
uv run python scripts/extension_acceptance.py run --snapshot benchmark-results/extension-v1/my-snapshot --output benchmark-results/extension-v1/my-offline
uv run python scripts/extension_acceptance.py report --snapshot benchmark-results/extension-v1/my-snapshot --evidence benchmark-results/extension-v1/my-offline --output benchmark-results/extension-v1/my-derived
```

默认 run 只有六题 Baseline/A 各一次；其余声明槽位保持未知，不能当作完整 90 行模型队列。report 输出 report.json、comparisons.json、带差值和区间的 report.md，覆盖矩阵保持未签署。

对有封存的证据，可给 report 追加 `--pr4-acceptance <seal.json>`，核对源码、种子、28 行覆盖及证据索引后绑定该 receipt 的 hash；篡改则拒绝。补算另开目录，记录原 actor_source_sha256、输入 hash、当前评分器 hash 和分析协议，标记 derived。历史 Trace 不反向改写，也不描述为重测当前 Harness。

本地 `benchmark-results/extension-v1/m5-analysis-development/attempt_0/derived/` 已对 PR 4 的封存脚本队列补算；对应评分器 bytes 保存在同层 scorers.tar.gz。这是历史数据上的新分析器开发证据，不是新的真实模型实验。

真实队列依然要求先明确批准三个已审阅种子，在新副本完成受控提交，冻结真实种子并绑定最终源码和安全证据。每个批准包仅能预留一次 90 行队列；未知用量或预算终止后保留全部行及消耗，不后台重试、换题或扩预算。report 只读取证据、写入全新派生目录，不修改原记录或访问模型凭据及服务。

# 固定与复现离线基线

`scripts/offline_regression.py` 将 2026-09-27 本地实验的四个临时脚本整理为一个仓库入口。
脚本只构造 ScriptedModel；三个 baseline 和六组消融使用 CLI，Session 对照使用相同
BenchmarkRunner，并在清理凭据与 profile 环境变量的独立进程中执行。

## 一次完整执行

在固定的实现提交上运行：

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts/offline_regression.py
uv run pytest
uv run python scripts/offline_regression.py
```

最后一条命令打印报告路径。默认每次创建新的 `benchmark-results/offline-<UTC>/`，
也可以用 `--output benchmark-results/my-baseline` 指定新目录。已有目录拒绝覆盖；
输出不能位于该结果目录之外。原始 JSON、JSONL、Trace 和任务副本保持 Git 忽略。

复现固定提交时，先检出该提交，再使用锁文件安装；实验执行期间不要修改源码或输入。
`metadata.json` 记录实际 HEAD、工作区是否有改动、Python/平台、完整 argv、起止 UTC
时间、退出码、manifest 摘要及源码摘要。摘要覆盖源码、测试、脚本、Benchmark 输入、
ADR、CI、项目约束、锁文件和用于对照的历史指标；不包含字节码缓存与运行产物。执行结束再次校验摘要，
防止运行中修改输入却把结果归属到旧版本。只有工作区干净时，HEAD 才能单独标识实现。

## 实验与审计口径

- smoke/core/stress baseline 各重复两次；core/stress 的 Context、工具输出、重试与反馈
  消融各重复两次，沿用已有变体与任务预算。
- core/stress 的 MemorySessionJournal 和 JsonlSessionJournal 各做四轮配对，交替顺序。
  除 Journal 外保持模型、任务、工具、预算、Context 与计价一致。
- 成功判据必须通过；每个任务必须包含全部重复次数，确定性指标必须一致。耗时不参与
  确定性比较。Session 配对结果必须一致；每份 Trace 都回放并校验终态、Token 和成本。
- 对照 `docs/results/summary.json` 的旧离线指标，按重复次数归一累计值。指标漂移会
  失败退出，需要先解释变化，再另立基线，不能静默覆盖历史结果。
- 全部执行成功后生成 `audit-summary.json` 和 `report.md`，元数据状态才设为 `passed`。
  子命令失败、判据失败或审计失败会非零退出，保留日志并标记 `failed`。

当前完整流程为 1010 次任务执行、393 对重复任务和 112 对 Journal 对照。实际数量由
报告推导；脚本不把这三个数字当作通过依据。模拟计价沿用输入 $3、输出 $15 per 1M
tokens，实际模型 API 费用为 $0。该流程验证 Runtime 与指标口径，不证明真实模型能力。
repo/hard 的真实模型实验仍需独立授权的凭据与费用预算；不在这个离线入口中运行。

## 保存结果

历史报告保留原来的提交号、摘要和测量值，不用新结果覆盖。发布新的报告时，从本次
生成的 `report.md` 选取归档，并保留生成来源与实验条件；无需提交原始 Trace 或任务副本。
报告不自动声称质量门禁通过，门禁以相同提交的 CI 或上面的实际检查结果为准。

实现提交 `c64eade983ac47eb35803d062657b3462abf5357` 已在干净工作区完成整个流程，
测量结果见[固定提交的离线基线报告](results/offline-baseline-2026-09-27.md)。

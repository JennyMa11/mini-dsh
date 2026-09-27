# 免费离线回归与脚本模型消融（2026-09-27）

> 生成来源：`benchmark-results/offline-20260927T110938Z/emit_report.py`；输入为同目录的 JSON 报告、`audit-summary.json` 和 `metadata.json`。

本轮没有调用真实模型 API，实际 API 费用为 $0。脚本模型只验证 Runtime 行为、确定性与指标口径，不能证明真实模型任务能力。

## 代码与功能门禁

| 检查 | 结果 |
| --- | --- |
| `uv run ruff format --check .` | 通过 |
| `uv run ruff check .` | 通过 |
| `uv run mypy src tests` | 73 个文件，零错误 |
| `uv run pytest` | 331 个测试通过 |

离线选择回归发现并修复：环境变量 `MINIDSH_MODEL_PROFILE` 曾覆盖显式 `--provider scripted`。新增 run、benchmark、ablation 的回归用例，验证不会构造真实 provider；同一命令显式指定 scripted 与 model-profile 时拒绝冲突。

## 实验条件

- 模型：`ScriptedModel`；没有 API Key 传入实验进程。
- 单 Agent、本地后端、默认断网；工具与成功判据在临时工作区运行。
- 显式空配置文件，排除隐式 profile；进程环境采用 allowlist。
- 三套基线和六组消融各重复两次；只使用带 response script 的 smoke/core/stress。
- repo/hard 没有响应脚本，本轮不执行其真实模型评测；hard 的 fixture、参考解与留出判据离线门禁包含在完整功能回归中。
- 预算、fixture 摘要、成功判据和隔离配置由 manifest 固定；manifest 摘要在 metadata.json，逐任务版本在报告中。
- 报告中的 USD 是按输入 $3 / 输出 $15 per 1M tokens 折算的模拟值，用于复核旧归档，不是当前服务价格或真实账单。
- Git HEAD：`c86b00865e607786fb09138f1b3a0f732d70498e`；工作区包含未提交改动，不能仅以该提交复现。
- 实现与测试工作区 SHA-256：`137964adc7546d3bad71f139be5b8df36b6a84c3dd822f8684c0e6a4489f00fc`。

## 基线结果

| 套件 | 不同任务 | 执行次数 | 判据通过 | 平均估算 Token |
| --- | --- | --- | --- | --- |
| smoke | 1 | 2 | 100% | 208.00 |
| core | 25 | 50 | 100% | 749.88 |
| stress | 3 | 6 | 100% | 9,534.00 |

## 单变量消融

对 core 与 stress 分别运行 Context（6 个变体）、工具输出（3 个变体）、重试/反馈（4 个变体）。全部变体的脚本任务判据通过；core 的短历史不足以区分窗口与摘要，stress 才出现明显的 Token 差异。

| stress Context | 平均估算 Token | 相对 full_history |
| --- | --- | --- |
| full_history | 9,534.00 | +0.00% |
| sliding_window | 8,981.00 | -5.80% |
| summary_compaction | 9,233.67 | -3.15% |
| repo_retrieval | 12,617.00 | +32.34% |
| retrieval_summary | 12,317.00 | +29.19% |
| symbol_retrieval | 9,233.67 | -3.15% |

| stress 工具输出 | 平均估算 Token |
| --- | --- |
| unbounded_output | 32,136.00 |
| bounded_output | 9,511.33 |
| bounded_output_artifact | 9,534.00 |

输出上限在这三个 stress 任务的平均估算 Token 上降低 70.40%。这是上下文体量的变化，不能据此推断真实模型成功率。

权限消融中的 no_retry 与 single_retry 没有重试收益：这些固定脚本没有可恢复的瞬时错误，所以本轮不提供“重试提高成功率”的证据。basic/detailed feedback 的 Token 差异可观测。

## Session 日志对照

只替换 MemorySessionJournal 与 JsonlSessionJournal，其余模型、任务、预算、工具、Trace 和 Context 保持一致。core/stress 各做 4 轮，轮间交替 memory/jsonl 顺序。

| 套件 | 配对次数 | 内存 Harness 耗时中位数（ms） | JSONL Harness 耗时中位数（ms） | 配对增量中位数（ms） |
| --- | --- | --- | --- | --- |
| core | 100 | 3.284 | 4.017 | 0.771 |
| stress | 12 | 5.912 | 7.159 | 1.365 |

共 112 对任务，结果、Token、工具调用、权限与模拟成本均一致。配对 Harness 耗时增量整体中位数为 0.808 ms；包含 JSON 编解码、校验与 fsync 的整体实现成本，不能解释为单独磁盘 I/O 成本。该值仅是本机小样本观测，不作跨环境性能保证。

## 审计与复现

- 共 1010 次任务执行，1010 次判据通过，保留同数量的原始 Trace。
- 393 对重复任务的确定性指标无差异；墙钟耗时不要求相同。
- 所有基线与消融的确定性指标和旧归档一致，累计次数/模拟成本按重复次数归一；不比较历史墙钟耗时。
- 全部 Trace 回放的终态、Token 与模拟成本均与逐任务报告一致。
- 原始 JSON、JSONL、Trace 与生成脚本位于 `benchmark-results/offline-20260927T110938Z/`，该目录被 Git 忽略。
- 本报告为当前未提交工作区的验证记录；正式发布可复现基线前应固定实现提交并保存实验脚本。

在当前工作区重新执行（会更新该实验目录内的报告）：

```bash
python3 benchmark-results/offline-20260927T110938Z/runner.py
uv run python benchmark-results/offline-20260927T110938Z/journal_runner.py
uv run python benchmark-results/offline-20260927T110938Z/audit.py
uv run python benchmark-results/offline-20260927T110938Z/emit_report.py
```

与真实 API 有关的成功率、真实 Token 与真实成本，需要后续单独实测。本轮不替换已有真实模型报告。

# ADR 0005：消融实验以单变量、可执行判据和脚本化模型为约束

- 状态：Accepted
- 日期：2026-09-21

## 背景

"某个 Harness 设计更好"这类结论极容易被自证：只要换了任务、换了预算、或者采信模型的自我报告，任何改动都能显得有效。Mini-dsh 的定位是**研究 Harness 机制**，因此实验协议本身必须是一等公民，而不是事后补的一段脚本。

## 决策

### 1. 单变量

`AblationRunner` 对**同一份 manifest** 依次运行一组 `HarnessVariant`，每个变体只改一个字段：

```python
HarnessVariant("sliding_window", context_strategy="sliding", ...)   # 只改 Context
HarnessVariant("unbounded_output", tool_output_limit=200_000, artifact_offload=False)
HarnessVariant("basic_feedback", detailed_tool_feedback=False)
```

- 模型、任务集、预算、隔离配置、采样参数、环境全部保持一致。
- `base_agent` 变体必须与 `RuntimeOptions` 的默认值逐字段对齐；这一点由回归测试 `test_base_variant_matches_the_runtime_defaults` 守住——否则"基线"可能根本不是默认配置，整组对照失效。
- `AblationRunner` 拒绝重复变体名，并记录所用 manifest。

### 2. 可执行判据

成功由独立的 `success_argv` 进程退出码判定，**从不采信模型自报完成**。Runner 为每个任务复制临时工作区，Agent 结束后在副本中执行判据：

- 退出码 0 → 成功
- 非 0 → `verification_failed`
- 超时 → `verification_timeout`
- 被权限层拒绝 → `verification_denied`

### 3. 脚本化模型作为默认基线

内置 `ScriptedModel` 按预置响应序列驱动 Loop，使整条链路离线、确定性、可复现，且不需要 API Key。它把 `success_rate` 变成**Harness 完整性检查**（Patch 是否生效、判据是否可执行、状态机是否误判），而不是模型能力指标。

为让成本指标可信，脚本化响应**不预置 `input_tokens`**；Harness 用 `domain.estimate_tokens` 测量实际送入模型的上下文。这是关键修正：早期版本在响应里写死 Token 数，导致所有 Context 策略读数完全相同，消融失去意义。

### 4. 让变量可观测

专门构造 `stress` 套件（长历史 18 步、75 KB 输出、含被拒绝动作），因为 `core` 这类短任务上各策略差异被固定开销淹没。同时把被拒绝的调用改为发布 `tool_call_completed` 事件，使 `permission_denied` / `unsafe_blocked` 从恒为 0 变成真实计数。

### 5. 原始 Trace 入库

每次 Model Call、Tool Call、权限决策、Context 变换与状态转换都发布版本化事件并落 JSONL；报告只是可再生成的视图，原始 Trace 才是审计依据。

## 替代方案

- **用真实 provider 做基线**：更接近真实能力，但引入网络、成本、非确定性，无法在 CI 中回归，也无法离线复现。作为后续实验的补充而非基线。
- **只跑一次、不做对照**：无法区分"Harness 改进"与"随机波动"，拒绝。
- **让模型自评是否完成**：与"成功以可执行判据为准"冲突，拒绝。

## 后果

- 消融结果可复现：任何人 `./scripts/run-all-experiments.sh` 都能得到同一组数字。
- 结论范围被显式限定：脚本化模型下只能得出**成本 / 行为**结论（Token、成本、拦截计数），不能得出"更准确"的结论。这一点在 `docs/benchmark.md` 与 README 中被明确写出。
- 代价：需要为每个任务维护 fixture、判据与响应脚本；生成器（`benchmarks/build_core.py`、`build_stress.py`）负责保证可再生成。

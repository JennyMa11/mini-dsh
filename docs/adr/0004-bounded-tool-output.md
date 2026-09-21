# ADR 0004：工具输出有界化，大结果落盘为 artifact

- 状态：Accepted
- 日期：2026-09-21

## 背景

工具输出是 Context 膨胀最不可控的来源。一次 `read_file` 命中 75 KB 日志、一次测试失败打印上千行栈、一次 `git diff` 覆盖整个仓库，都会把后续每一步的输入成本推高一个数量级，并稀释真正有用的信号。

同时，输出本身可能有价值：模型不该看到全部内容，但也不该永久失去它——调试、审计和复现都需要原始结果。

## 决策

Tool Runtime 在产出 Observation 前统一处理输出体积：

```python
serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
if len(serialized) <= self._max_output_chars:  # 默认 20_000
    return ToolObservation(..., result)
```

超限时：

- 若配置了 `LocalArtifactStore`，把**完整原文**写入 `<workspace>/.minidsh/artifacts/`，路径回填到 `Observation.artifacts`。
- 送入模型的结果替换为显式截断结构：

```json
{ "preview": "<前 20000 字符>", "original_characters": 75123 }
```

- `Observation.truncated = True`，事件流与报告都记录该事实。

因此：**截断是显式的、可观测的、可回溯的**，模型知道内容被截断，Harness 知道原文在哪。

## 替代方案

- **不限制输出**：实现最简，但实测把 `stress` 任务的平均 Token 从 9 511 推到 32 136（3.38× 成本），拒绝。
- **直接截断且丢弃原文**：省成本，但调试时无法回答"模型到底看到了什么 / 漏了什么"，拒绝。
- **在模型侧摘要输出**：需要额外模型调用、非确定性、且引入成本，与"Harness 不该替模型做判断"的边界冲突，拒绝。
- **把 artifact 注入后续 Context**：会重新引入膨胀，且让模型可能反复读取大文件，拒绝；artifact 只作为外部引用与审计材料存在。

## 后果

- 成本可控：`unbounded_output` vs `bounded_output` 的对照实验证明输出上限是最大的单点成本杠杆。
- 可观测：`truncated` 与 `original_characters` 让"信息被丢掉多少"成为可统计事实。
- 代价：`LocalArtifactStore` 会在工作区写入 `.minidsh/artifacts/`，需要 `.gitignore` 覆盖，并注意它仍受 `WorkspaceSandbox` 路径约束（落在工作区内，不逃逸）。
- 与 Context 策略解耦：无论用哪种 Context 策略，输出有界化都在 Tool 层独立生效，两个变量可分别消融。

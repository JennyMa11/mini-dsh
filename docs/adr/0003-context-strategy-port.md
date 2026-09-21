# ADR 0003：Context 策略是端口，原始 Trace 是事实源

- 状态：Accepted
- 日期：2026-09-21

## 背景

Coding Agent 的 Context 管理最容易与主循环耦合：一旦"如何裁剪历史"写在 Loop 里，就无法回答"换一种 Context 策略会不会更便宜、更准"。Mini-dsh 的核心研究目标之一就是量化 Context 设计的影响，因此裁剪逻辑必须可替换、可观测、可对照。

同时存在一个正确性风险：摘要与检索结果都是**有损**的派生数据。如果让摘要覆盖原始记录，一次错误的压缩就永久污染了事实，实验也无法审计。

## 决策

把 Context 定义为纯函数式端口：

```python
def build(self, history: tuple[Message, ...], task: str) -> ContextView: ...
```

- `history` 是只读输入，策略**只能产出新视图**，不得改写历史。
- 返回值 `ContextView` 必须自报 `strategy` / `version` / `estimated_tokens` / `omitted_messages` / `metadata`，使压缩与丢弃可观测。
- 原始事件流与完整消息历史是事实源；摘要、窗口、检索结果都是**可再生成的派生视图**。
- 所有策略共用同一接口，因此 `FullHistory` / `SlidingWindow` / `SummaryCompaction` / `RepoRetrieval` / `Retrieval+Summary` / `SymbolRetrieval` 可以在同一任务、同一模型、同一预算下公平对比。
- 检索被实现为**装饰器**（`RetrievalContext` 包住任意基础策略），而不是并列的第七种策略，从而避免"压缩 × 检索"的组合爆炸。
- 永久保留系统约束、当前任务、关键决策、未解决错误与已修改文件清单；内置策略只裁剪中间过程，不触碰头两条消息。

## 替代方案

- **在 Loop 里 if/else 选策略**：改动小，但无法注入自定义策略，也无法记录策略版本，拒绝。
- **引入向量库做检索**：检索质量可能更高，但引入外部服务、非确定性排序和网络依赖，破坏离线可复现，首版拒绝。改用词面重叠 + AST 符号定位两个纯本地检索器。
- **用摘要替换原始历史**：省内存，但破坏事实源，实验不可审计，拒绝。

## 后果

- Loop 保持策略无关，Context 成为独立可测单元（`tests/test_context.py`）。
- 每次模型调用都会带 `strategy` 与 `version`，Trace 可回溯"这一步用的什么策略"。
- 代价：每个策略都要自己实现 Token 估算与元数据上报，重复度略高；用共享的 `domain.estimate_tokens` 与统一 `ContextView` 缓解。

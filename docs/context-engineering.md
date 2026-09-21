# Context Engineering

Context 是 Coding Agent 里最容易被"顺手写死"的部分：一旦策略和 Agent Loop 耦合，就再也做不了对照实验。Mini-dsh 把 Context 抽象成一个纯函数式端口，让六种策略共用同一接口，从而可以在同一任务、同一模型、同一预算下比较它们的 Token 成本与信息保真度。

## 1. 契约

```python
@dataclass(frozen=True, slots=True)
class ContextView:
    messages: tuple[Message, ...]  # 送入模型的派生视图
    strategy: str  # 策略名，进 Trace 与报告
    version: str  # 策略版本，保证可复现
    estimated_tokens: int  # Harness 侧确定性估算
    omitted_messages: int = 0  # 被丢弃/压缩的消息数（可观测）
    metadata: dict[str, JsonValue] = field(default_factory=dict)
```

策略侧只需实现一个方法：

```python
class ContextStrategy(Protocol):
    name: str
    version: str

    def build(self, history: tuple[Message, ...], task: str) -> ContextView: ...
```

三条硬约束：

1. **原始 Trace 是事实源**。`history` 是只读输入，策略只能产出新视图，不能反向改写历史。摘要、窗口、检索结果都是**可再生成的派生视图**。
2. **必须自报账**。每次 `build` 都要返回策略名、版本、Token 估算、丢弃数量与元数据；压缩和丢弃必须可观测，不能静默发生。
3. **永久保留关键信息**。系统约束、当前任务、关键决策、未解决错误、已修改文件清单不参与压缩。这是所有内置策略的共同不变量——它们只裁剪中间过程，不动头两条消息（system + task）。

Token 估算用共享的确定性实现（`domain.estimate_tokens`），字符数 / 4 向上取整，含工具调用参数；它不绑定任何 tokenizer，保证离线可复现：

```python
def estimate_tokens(messages: tuple[Message, ...]) -> int:
    characters = sum(len(message.content) for message in messages)
    tool_characters = sum(
        len(call.name) + len(str(call.arguments))
        for message in messages
        for call in message.tool_calls
    )
    return max(1, (characters + tool_characters + 3) // 4)
```

## 2. 六种内置策略

| 策略 | 类 | 机制 | 保留什么 | 代价 |
| --- | --- | --- | --- | --- |
| `full_history` | `FullHistoryContext` | 原样透传全部消息 | 一切 | Token 随步数线性增长 |
| `sliding_window` | `SlidingWindowContext` | 钉住前 2 条，保留最近 `window_size`（默认 20）条 | 系统约束 + 任务 + 近期动作 | 中段历史整段丢失 |
| `summary_compaction` | `SummaryCompactionContext` | 把中段压缩成有界摘要（`summary_max_chars` 默认 4000），保留最近 `recent_messages`（默认 12）条 | 头 + 有界摘要 + 近期原文 | 摘要丢细节，且自身耗 Token |
| `repo_retrieval` | `RetrievalContext(FullHistoryContext, RepositoryRetriever)` | 全文透传 + 按任务词面相关度注入最多 5 个文件片段 | 全部历史 + 相关代码 | 额外注入推高输入 Token |
| `retrieval_summary` | `RetrievalContext(SummaryCompactionContext, ...)` | 压缩历史 + 检索注入 | 有界摘要 + 相关代码 | 压缩损失 + 注入成本 |
| `symbol_retrieval` | `RetrievalContext(FullHistoryContext, PythonSymbolRetriever)` | 用 AST 定位函数/类，只注入符号体 | 全部历史 + 精确定位代码 | 仅覆盖 Python |

### 检索实现

两个检索器都不依赖向量库，纯本地、确定性：

- `RepositoryRetriever`：词面重叠打分。文件内容命中数 + 路径命中加权（路径命中 ×2），按 `(-score, path)` 排序取 top-k；跳过 `.git` / `.venv` / `node_modules` / `dist` / `build`，单文件超 256 KB 跳过，单块截断到 4000 字符。
- `PythonSymbolRetriever`：`ast.walk` 遍历 `ClassDef` / `FunctionDef` / `AsyncFunctionDef`，按符号名与任务词的重叠打分（命中 +2 基础分），只切出符号体本身，因此定位更准、块更小。

`RetrievalContext` 是装饰器而非替代品：它包住任意基础策略，在 `build` 结果上追加一条只读的检索消息，并把 `retrieved_chunks` / `retrieved_paths` 写进 `metadata`。因此"压缩 + 检索"这类组合不需要新类。

## 3. 选型

| 场景 | 建议 | 理由 |
| --- | --- | --- |
| 步数少（< 15）、预算宽松 | `full_history` | 零信息损失，成本可接受 |
| 长历史、只关心最近动作 | `sliding_window` | 最省 Token，实现最简 |
| 长历史且中段有结论 | `summary_compaction` | 保住结论、丢掉过程 |
| 需要跨文件理解、上下文稀疏 | `repo_retrieval` | 补上任务相关代码 |
| 长历史 + 需要跨文件 | `retrieval_summary` | 两者兼顾，最贵 |
| Python 仓库、目标符号明确 | `symbol_retrieval` | 注入块最小、最精准 |

## 4. 实验证据

`stress-long-history`（18 步、16 个模块）上的实测（脚本化模型、seed 0、$3/$15 per million）：

| 策略 | 平均 Token | 总成本 | 相对 `sliding_window` |
| --- | --- | --- | --- |
| `sliding_window` | 8981.0 | $0.090045 | — |
| `summary_compaction` | 9233.7 | $0.092319 | +2.8% |
| `symbol_retrieval` | 9233.7 | $0.092319 | +2.8% |
| `full_history` | 9534.0 | $0.095022 | +6.2% |
| `retrieval_summary` | 12317.0 | $0.120069 | +37.1% |
| `repo_retrieval` | 12617.0 | $0.122769 | +40.5% |

读数：**检索类策略在长历史上是净增 Token 的**——它把相关文件重新注入 Context，用输入成本换信息密度。`symbol_retrieval` 之所以比 `repo_retrieval` 便宜 27%，正是因为它按 AST 只切符号体，而不是塞整个文件的前 4000 字符。

`core` 任务集上差距被压平（`full_history` 749.9 vs `repo_retrieval` 1355.1）：任务短、历史浅时，检索注入的固定开销占比高而收益低。**这正是"任务太小就没有区分度"的证据**，也是我们专门造 `stress` 套件的原因。

注意：本组实验的成功率恒为 100%（脚本化模型），因此只能得出**成本结论**，不能得出"某策略更准确"的结论。要下后者必须换真实 provider。

## 5. 新增策略

1. 在 `context/strategies.py` 里加一个 frozen dataclass，实现 `build(history, task) -> ContextView`，填好 `name` / `version` / `omitted_messages` / `metadata`。
2. 需要检索就实现 `Retriever` 协议（`search(query, *, limit)`），或直接复用现有检索器。
3. 在 `context/__init__.py` 导出。
4. 在 `benchmark/ablation.py` 的 `CONTEXT_VARIANTS` 里加一个变体，让它进入消融矩阵。
5. 补一个单元测试断言 `estimated_tokens` 与 `omitted_messages` 的关系，再跑一次消融确认它与其它策略可分辨。

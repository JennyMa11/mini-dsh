# ADR 0001：端口驱动的单进程 Runtime

- 状态：Accepted
- 日期：2026-09-21

## 背景

Mini-dsh 需要在不修改核心 Agent Loop 的情况下替换模型、工具、Context、Session、Permission 与事件处理，同时保存可复现实验所需的事实 Trace。

## 决策

采用端口驱动的单进程架构：

```text
CLI / Benchmark
      |
Composition Root ---- Plugin Manager / Service Registry
      |
Agent Loop --> Model | ContextManager | ToolExecutor | EventSink | SessionStore
                                      |
                         Tool Registry / Permission Policy
                                      |
                         Workspace / Process adapters
```

领域对象使用不可变 dataclass；Pydantic 只出现在 JSON、模型响应、工具参数和 Benchmark manifest 等不可信边界。所有模型产生的副作用都经 Tool Runtime 的 Schema、授权和错误边界。

插件先声明 `provides/requires`，Runtime 在执行插件代码前检查重复服务、缺失依赖和环，并按逆序回滚启动失败。热替换只允许通过显式的 `replace_at_boundary` 发生在 Agent 步骤之间。

## 替代方案

- 全局 Service Locator：调用方便，但依赖不可见、测试隔离差，拒绝。
- 将厂商 SDK 和工具直接写入 Loop：初期代码少，但无法进行 Harness 消融，拒绝。
- 首版采用多进程/分布式 Runtime：超出验证 Harness 机制所需范围，延后。

## 后果

构造函数和组合根更显式，适配器数量增加；作为交换，Loop、测试和 Benchmark 不依赖具体模型厂商或操作系统命令。

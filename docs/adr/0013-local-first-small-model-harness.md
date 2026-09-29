# ADR 0013：本地小模型执行路径

- 状态：Accepted

## 背景

现有模型端口和工具链支持远端 Chat Completions 与脚本模型，但本机 16GB Apple Silicon 的 Qwen3 4B/8B 对输入长度、输出格式和重复动作更敏感。仅增加模型配置无法测量 Harness 对成功率的贡献。

## 决策

MLX-LM 是可选的 Apple Silicon adapter，运行时只接受已下载的本地权重目录。adapter 用模型 tokenizer 计数，并将单动作 JSON 解析为既有 `ModelResponse`/`ToolCall`；Schema 和权限校验仍由 Tool Runtime 完成。原有 OpenAI-compatible 与 Ollama 接口保持不变。

新增预算 Context 视图与逐级仓库工具：先列路径与候选匹配数，再按行读取。原始 Session/Trace 不被压缩；预算策略保留任务和最新 Plan State，并以完整工具回合为单位取舍，记录被省略的消息位置。模型可见的工具结果由同一 Observation 导出；大结果仍写入受控 artifact。

本地模式可选启用独立 Plan State 和重复动作检测：相同工具、参数和结果连续出现三次，或同类测试错误累计三次时提示改策略；六次时以 `loop_stuck` 失败结束。失败和权限拒绝不会触发有副作用工具的自动重试。

Benchmark `matrix` 顺序运行固定 4B/8B 权重版本与基线/优化 Harness 的四组对照。每组使用相同 manifest、预算、重复次数和留出判据；成功率按测试判定，推理指标缺失时显式记为 `null`。

## 替代方案与后果

- 只经本地 HTTP 服务调用 MLX：接入简单，但难以取得逐调用 TTFT、吞吐和内存读数，因此保留为兼容路径。
- 改造所有 provider 的工具调用格式：会破坏现有接口与实测基线，因此 JSON 动作协议仅用于 MLX。
- 默认读取整个仓库：输入预算不可控，因此改为候选定位与按需读取。

MLX 依赖只在 `mlx` extra 中安装，非 Apple Silicon 平台仍可运行核心测试。GUI 与可选 Multi-Agent 保持兼容，不属于本次对照实验。

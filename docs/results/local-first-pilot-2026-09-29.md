# Local-first 4B/8B 试验（2026-09-29）

## 条件

- 设备：Apple M5，16GB 统一内存；MLX-LM 本地推理，贪心采样，关闭 Qwen3 thinking。
- 模型：`Qwen3-4B-MLX-4bit` revision `52a5ab34fa604bc8af6d3ce0cac0cab10b7eb495`；`Qwen3-8B-MLX-4bit` revision `383413e909f3bc5303ce195ebbdf0339c5a1a2a3`。权重从本地已固定的 snapshot 目录加载。
- 输入：`benchmarks/local/smoke-manifest.json` 中一个修复加法函数的任务；初始 fixture 版本和留出测试 overlay 的摘要由 manifest 固定。Agent 运行期间看不到留出判据。
- 预算：12 步、30,000 Token、24 次工具调用、180 秒；MLX 输入上限 8192 Token，单次生成上限 1024 Token。
- 每格重复 2 次。Baseline 使用完整历史与原有工具；Optimized 使用预算 Context、渐进仓库工具、结果规范化、Plan State、Loop Guard 与失败恢复。两者使用同一模型动作协议与 20,000 字符工具输出上限。
- 代码基于 `814e44e233d8bb06e9638e78d0aa0963620c3a8f` 的未提交工作区改动；实验原始报告在本机 `benchmark-results/local-smoke-matrix-r2.json`，并保留各次 Trace。此快照尚未固定为提交，因此跨机器精确复现还需固定实现版本。

## 留出判据结果

| 模型 | Harness | 成功 | 平均 Token | 平均步骤 | 平均工具调用 | 平均墙钟时间 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3-4B Q4 | Baseline | 2/2 | 17,164 | 4 | 3 | 21.0 秒 |
| Qwen3-4B Q4 | Optimized | 2/2 | 14,366 | 6 | 5 | 22.3 秒 |
| Qwen3-8B Q4 | Baseline | 2/2 | 20,400 | 6 | 5 | 54.9 秒 |
| Qwen3-8B Q4 | Optimized | 2/2 | 7,445 | 4 | 3 | 23.0 秒 |

这个任务上，4B Optimized 的 Token 比 4B Baseline 少 16.3%，8B Optimized 比 8B Baseline 少 63.5%。4B Optimized 与 8B Baseline 都完成留出判据，但这里只是一个较小任务上的两次重复；不能据此断言 4B 在一般 Coding Agent 任务中达到 8B 的成功率。完整成功率结论需要更大的固定任务集和多次重复。

各格平均 TTFT 分别为 2.10、2.12、6.22、3.87 秒；MLX 报告的峰值内存分别为 3.33、3.05、5.45、5.23 GB。该内存是 MLX 的进程读数，不是 macOS 整机峰值。模型加载发生在计时任务之外，报告中的 `load_ms` 单独记录。

## 复跑

```sh
uv run --extra mlx mini-dsh matrix benchmarks/local/smoke-manifest.json \
  --model-4b /path/to/52a5ab34fa604bc8af6d3ce0cac0cab10b7eb495 \
  --model-8b /path/to/383413e909f3bc5303ce195ebbdf0339c5a1a2a3 \
  --verify-command python --repeat 2 \
  --output benchmark-results/local-smoke-matrix-r2.json
```

## 七阶段单变量消融

同一留出任务、同一 8B 权重，每阶段运行一次；原始报告在本机 `benchmark-results/local-smoke-ablation-8b.json`。阶段依次改变工具输出上限、结果规范化、预算 Context、渐进工具、Plan State/Loop Guard 与同类错误恢复。

| 阶段 | 留出判据 | Token | 步骤 |
| --- | ---: | ---: | ---: |
| local_baseline | 通过 | 20,400 | 6 |
| bounded_output | 通过 | 20,400 | 6 |
| normalized_output | 通过 | 20,547 | 6 |
| budget_context | 通过 | 20,547 | 6 |
| progressive_loading | 通过 | 7,120 | 4 |
| loop_guard | 通过 | 7,445 | 4 |
| error_recovery | 通过 | 7,445 | 4 |

在这个输出很短、历史很浅的任务上，输出上限和预算 Context 没有触发，结果规范化略增 Token。渐进工具改变了 Agent 的定位路径，少走两步；Plan State 增加少量提示词。同类错误恢复没有被触发。阶段差值是这一条轨迹的结果，不能替代跨任务成功率测量。四格矩阵比较组合 Harness，不能把组合收益全归因给渐进工具。

```sh
uv run --extra mlx mini-dsh ablation benchmarks/local/smoke-manifest.json \
  --suite local_first --provider mlx \
  --model /path/to/383413e909f3bc5303ce195ebbdf0339c5a1a2a3 \
  --verify-command python --repeat 1 \
  --output benchmark-results/local-smoke-ablation-8b.json
```

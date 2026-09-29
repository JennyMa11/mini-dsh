# 本地小模型实验

目标设备为 Apple M5、16GB 统一内存。主模型是 Qwen3-8B-MLX-4bit，轻量基线是 Qwen3-4B-MLX-4bit。权重由用户显式下载到 Hugging Face 缓存；运行中不下载。依赖使用 `uv sync --extra mlx`。

```sh
uv run --extra mlx hf download Qwen/Qwen3-4B-MLX-4bit --revision 52a5ab34fa604bc8af6d3ce0cac0cab10b7eb495
uv run --extra mlx hf download Qwen/Qwen3-8B-MLX-4bit --revision 383413e909f3bc5303ce195ebbdf0339c5a1a2a3
```

把以下路径替换为下载命令返回的 `snapshots/<40 位 revision>` 目录：

```sh
uv run --extra mlx mini-dsh run "修复测试" --provider mlx --model /path/to/snapshot --workspace /path/to/repo --allow-write --allow-shell --allow-command python
uv run --extra mlx mini-dsh matrix benchmarks/repo/manifest.json --model-4b /path/to/4b/snapshot --model-8b /path/to/8b/snapshot --verify-command python --repeat 3 --output benchmark-results/local-matrix.json
uv run --extra mlx mini-dsh ablation benchmarks/local/manifest.json --suite local_first --provider mlx --model /path/to/8b/snapshot --verify-command python --repeat 3 --output benchmark-results/local-ablation.json
```

单模型运行默认启用 `budget` Context 与本地 Loop Guard，输入预算为 8192 Token，最多生成 1024 Token。`--context-input-budget` 可调整输入上限；`--no-local-first` 可关闭 Plan State 与 Loop Guard。`list_files`、`search_code`、`find_symbol` 分别提供文件概览、按文件汇总的候选结果及 Python 符号行号；`read_file` 用 `start_line` 和 `end_line` 精确读取。旧 `search` 与 `git` 工具继续可用。

`matrix` 的 baseline 使用现有完整历史和原有工具集，optimized 启用预算 Context、渐进工具、结果处理与 Loop Guard。两组均使用同一 MLX 单动作协议、20,000 字符工具上限、相同任务和权限。四组顺序执行，原始 Trace 和逐任务结果保存在输出目录。报告中的成功率以留出判据为准；脚本模型先前得到的 3.38 倍 Token 差不能当作本地 Qwen 的收益。

`local_first` 消融按单变量顺序加入工具输出上限、结果规范化、预算 Context、渐进读取、Plan State/Loop Guard 与同类错误恢复；模型、任务、预算与留出判据保持一致。

本机四格试验的条件、留出判据结果与样本量限制见 [2026-09-29 试验记录](results/local-first-pilot-2026-09-29.md)。

26 任务、三次重复和五阶段消融采用独立的 [正式实验协议](local-formal-study.md)。pilot 结果不与该协议下的任务成功率合并。

MLX `peak_memory_gb` 是 MLX 进程报告的峰值，并非 macOS 整机峰值。推理指标从 Trace 中逐调用汇总；没有读数时为 `null`。当前基线不自动开启网络权限，模型权重与原始 Trace 不入库。

Ollama 和 llama.cpp 走原有 OpenAI-compatible adapter，可分别选 `--model-profile ollama` 与 `--model-profile llama_cpp`，并通过 `MINIDSH_MODEL` 指定已加载的模型。它们不产生 MLX 专属内存读数。

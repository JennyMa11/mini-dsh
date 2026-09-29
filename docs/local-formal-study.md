# Local-first 正式实验协议 v1

这份协议把 [两任务 pilot](results/local-first-pilot-2026-09-29.md) 扩展为冻结的任务集。语料、模型、运行预算和统计方法在测量前确定；正式报告只在一个所选阶段全部运行完成后生成。pilot 数字不并入正式样本。

## 任务与离线门禁

`benchmarks/local_formal/v1/manifest.json` 固定 26 个任务：Bug fix 8、Small feature 6、Refactor 4、Test repair 4、Repo navigation 4。6 个 repo 任务与 4 个 hard 任务从既有生成器复制，另有 16 个任务由 `benchmarks/build_formal.py` 生成。每个任务固定目标、工作区、测试命令、预算、留出判据和完整 SHA-256 输入哈希；参考解也有完整哈希。

模型只能看到工作区内的完整文字规格和可运行的可见测试。Runner 在模型结束后才将 `verification/<task_id>` 覆盖到临时工作区，再执行 `python run_verification.py`。Test repair 判据要求生产源码字节不变，修复后的测试通过正确实现并杀死指定错误实现。Repo navigation 判据要求 `answer.json` 正确且源码字节不变。其他任务用留出测试判定。`tests/test_formal_corpus.py` 对每个原始输入执行失败检查，并对参考解执行通过检查；这项门禁不依赖模型、网络或用户主目录。

生成与检查：

```sh
uv run python benchmarks/build_formal.py
uv run pytest -q tests/test_formal_corpus.py tests/test_formal_study.py
```

测量前冻结语料和 Harness 代码；不要根据模型试跑结果删改任务。若修正语料，递增协议版本并从头运行。当前语料是项目自建、规模较小的 curated coding tasks，不能代表一般编码任务分布。

## 固定运行条件

模型是本机已下载的 Qwen3 4B/8B MLX 4bit 快照目录，目录名必须为 40 位 revision。MLX 使用 greedy decoding、关闭 thinking、8192 输入 Token 上限、每次最多生成 1024 Token。四格矩阵每个任务每格运行 3 次，共 312 次；重复用于观察稳定性，统计推断以任务为单位。

统一预算为 24 步、150,000 总 Token、48 次工具调用和 420 秒。统一测试命令为 `python -m unittest discover -s tests`，验证命令为 `python run_verification.py`。模型可用工具固定为 `read_file`、`search`、`patch_file`、`write_file`、`test`；渐进检索阶段另开放 `list_files`、`search_code`、`find_symbol`。任意 Shell 不暴露给模型，验证器只允许 `python`，网络默认关闭。Runtime 使用本地工作区沙箱；它不提供操作系统级隔离，因此这些任务只能使用可信的本地模型和受控语料。

所有阶段都开启结构化工具结果归一化与受控 artifact offload，避免把它们误算成消融增益。矩阵 Baseline 对应 `bounded_20k`，Optimized 对应 `loop_guard_recovery`。8B 五阶段消融为：

| 阶段 | 相对前一阶段的变化 |
| --- | --- |
| `raw_200k` | 20 万字符工具结果上限、完整历史、基础工具 |
| `bounded_20k` | 工具结果上限降到 2 万字符 |
| `progressive_repo` | 加入渐进式仓库检索工具 |
| `budget_context` | 使用 Token-budget Context |
| `loop_guard_recovery` | 加入轻量 Plan State、Loop Guard 与恢复策略，作为一个组合阶段 |

最后一个阶段只支持“组合机制”结论，不单独归因于 Loop Guard 或恢复策略。对照中的采样、任务、预算、验证器、权限和归一化策略保持一致。

## 运行与恢复

```sh
uv run mini-dsh formal-study benchmarks/local_formal/v1/manifest.json \
  --model-4b /path/to/Qwen3-4B-MLX-4bit/snapshots/<40-character-revision> \
  --model-8b /path/to/Qwen3-8B-MLX-4bit/snapshots/<40-character-revision> \
  --phase all --repeat 3 \
  --output-dir benchmark-results/local-formal-v1
```

也可先运行 `--phase matrix`，再运行 `--phase ablation`；两者使用相同输出目录。调度按重复轮、模型、任务排列：同一任务的两个矩阵配置相邻运行，任务与重复轮交替先后顺序，每轮旋转模型先后顺序，并在轮次边界重新加载权重。每个完成的运行立即写入 `results.jsonl` 并 `fsync`；重启同一命令会跳过已有键。若协议哈希不符、键重复或 Trace 丢失，恢复会拒绝继续。

`protocol.json` 记录代码树摘要、manifest 哈希、模型快照、阶段参数与环境；每次运行有单独 JSONL Trace。`report.json` 只汇总完整的实验格，原始逐次结果仍是事实源。Benchmark 工作副本、模型缓存、Trace 与原始结果不提交仓库。

## 指标与分析

按所有运行报告成功率、平均与 P50 Token、耗时、步骤、工具调用；另给成功运行子集与类别分层结果。记录工具重试、模型格式重试、Loop Guard 触发、失败类型、Prompt/Output Token、TTFT、按 Token 加权的 Prefill/Decode 速度、MLX allocation peak、macOS `phys_footprint` 采样峰值与前后系统空闲内存百分比。`phys_footprint` 是进程口径，不能当作整机统一内存占用；MLX peak 是 MLX 分配口径，也不能与进程峰值相加。

矩阵比较和相邻阶段比较按任务与重复编号配对，使用任务聚类 bootstrap 95% 区间（固定随机种子 42）；3 次重复不作为 3 个独立任务。成功以留出判据为准，模型宣称完成本身不计成功。报告应同时陈列失败和耗时上升的配置，避免只报告 Token 改善。

# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)。

## 未发布

- 新增 hard 套件的两策略 Context 对照入口，交替任务内的策略顺序，保留重复采样、
  失败样本和完整 Trace；不改动核心 Loop 或既有 BenchmarkRunner。
- 新增连续终端会话和独立本地 GUI，支持恢复、取消与显式运行设置。
- 新增可选只读 Sub Agent 协作、团队预算与有界并发；核心 Loop 保持单 Agent。
- 新增显式 Runtime 扩展、Session 消息事实日志、Loop 工厂端口和可选 MCP HTTP adapter；
  取舍与兼容边界记录在 ADR 0007–0012。
- 将离线回归、Journal 对照和 Trace 审计整理为 `scripts/offline_regression.py`，记录实际
  提交号、工作区状态、源码与输入摘要；新结果使用独立目录，不覆盖历史报告。
- 修正文档门禁：当前说明跟随测试规模，历史测量报告保留当时的数字。

## [0.3.2] - 2026-09-22

**第一次测到能力。** 到 `0.3.1` 为止所有套件的成功率都是 100%，而这不是"模型强"，是**判据的问题**：`core` / `stress` / `repo` 的判据都是工作区里的可见测试，也就是 Agent 能读、能改的那份文件——测的是**顺从**，不是**正确**。本节给 harness 加上留出判据（判据在 Agent 结束之后才落地），建了第一套非饱和的任务集，并交付第一次低于 100% 的实测结果。同一轮还修掉了这次运行暴露出来的第二个失败归因缺陷。

### Added

**留出判据（held-out verification）**

- manifest 新增 `verification_overlay` 与 `verification_commit`：Runner 在 **Agent 结束之后、`success_argv` 执行之前**把 overlay 目录覆盖进工作区。时机是全部关键——早一步判据可读可改，晚一步判据不参与判定。声明了 overlay 却指不到目录的 manifest 在验证期直接拒绝，而不是静默退化成弱判据。
- overlay 落地失败（例如 Agent 在 overlay 期望目录的位置留了一个文件）只让该任务失败（`verification_overlay_failed`），不再抛出中断整轮扫描、丢掉已经测得的全部结果。

**`hard` 套件：4 个模块级规格实现任务**

- 区间合并、令牌桶限流、分层配置解析、RFC 4180 记录读取。每个工作区只带规格的 smoke 子集，任务目标明说"`tests/` 只是 smoke 子集，docstring 才是判据"。**被留出检查覆盖的每个契约点都写在 docstring 里**，所以失败意味着"没有实现写明的契约"，而不是"被藏了需求"。
- 判据脚本 `run_verification.py` 本身也来自 overlay，它把发现范围钉死在 `verification_tests/`，并**断言收集到的用例数不低于声明值**：`unittest discover` 在空目录上退出码是 0，不设这条下限的话"删掉测试目录"就是最省事的作弊方式。
- `tests/test_hard_suite.py` 在离线、不调模型的前提下双向门禁每个任务：出厂 fixture 必须**失败**（否则任务一开始就是解好的），随包参考解必须**通过**（否则低成功率无法与"任务本身做不了"区分，0% 会变成一个无法解读的数字）。

**`hard` 套件实测结果**

- 4 任务 × 5 次重复 = 20 次运行，`deepseek-flash`，thinking 关闭，off-peak 费率 $0.15 / $0.6 / $0.003：**成功率 95%（19/20）**，归档在 [`docs/results/hard.md`](docs/results/hard.md)。这是本仓库第一次给出低于 100% 的成功率——不是因为模型变弱了，是因为判据第一次不再由 Agent 自己维护。
- 唯一的失败是 `hard-interval-merge` 第 2 次重复：17 步（上限 24）、21 次工具调用（上限 48）**全部成功**，累计 151,854 Token 撞上 15 万 Token 上限。**绑定约束是 Token 而不是步数**——它只走到 17/24 步，步数预算从未接近。
- `summary.json` 的 `real_model` 条目新增 `tasks` 逐任务汇总（`archive-results.py` 的 `_task_rollup`）：聚合值区分不了"一个任务每次都失败"和"五个任务各失败一次"，而这是两个不同的结论。**没成功过的任务其 `cost_per_success_usd` 记 `null` 而不是 0**，否则最难的会显示成最便宜的。
- README 新增"留出判据套件（`hard`）"一节，表格放在 `<!-- hard-suite-results:start -->` 标记内；[`docs/real-model-evaluation.md`](docs/real-model-evaluation.md) 新增第 11 节；[`docs/benchmark.md`](docs/benchmark.md) 补上 `verification_overlay` 的协议、执行顺序与新增任务的流程。

### Fixed

- **模型产出不可用响应时，失败被归到 `environment` 层。** `ModelAdapterError` 同时覆盖"provider 挂了"和"模型吐了无法解析的响应"，而 `_provider_failure` 按异常类名匹配，于是两者都被记成 `provider_error`——一个读起来就是"不怪模型"的层。现在 Agent Loop 把 adapter 的 `code` 写进 `failure_reason`（`ModelAdapterError[invalid_tool_arguments]: ...`），分类法按 code 判定：`bad_response` 与 `invalid_tool_arguments` 归 `agent` 层（`model_reasoning_failure`），其余 code 仍是环境问题。**这是第三次由真实模型运行照出、脚本模型结构性照不到的归因缺陷**（前两次是截断 400 与错误响应体被丢弃）。
- 没有 code 的旧 trace 仍按类名判定，保持向后兼容——归档的 trace 不会因为格式演进改变诊断。

### Changed

- **生成型 benchmark 目录加入 lint 排除。** `benchmarks/**/fixtures`、`benchmarks/**/verification`、`benchmarks/**/solutions` 都是生成的数据，代码在 `benchmarks/build_*.py`（它仍然被 lint）。在这里做格式化还会与"重新生成并逐字节比对"的门禁打架。

### Tests

- 229 个测试（+13）：4 个覆盖 overlay 契约（overlay 在运行前落地、从未落地、缺 overlay 不被拒绝、相对路径按工作目录解析），4 个覆盖 `hard` 套件门禁（出厂 fixture 失败、参考解通过、用例数下限、生成物逐字节可复现），3 个覆盖失败归因（Loop 记录 code、taxonomy 按 code 分层、Runner 端到端落在 `agent` 层），2 个把 README 的 `hard` 表**逐格**与归档比对。
- 全部 13 个新门禁都通过"故意改坏实现、确认失败、再改回"验证过会真的失败。`hard` 表门禁另用七种破坏方式实测：改 Token、改成功率、**交换两列**、改合计、删掉一行、删掉费率、改每次成功成本——七种都被拦下。

### 说明

- **`harness: 1` 不等于"模型没错"。** 失败层分解是 `success 19 / harness 1 / agent 0 / environment 0`。分类法里 `harness` 的含义是"运行时按规格执行了"（预算确实按规格执行了），它回答的是**运行时是否被免责**，不是"模型是否失败"。按能力读，这次就是一次**未收敛**。`agent: 0` 只说明没有任何失败可以归因到"harness 坏了"或"provider 挂了"。这一条留在文档里而不是改分类法：`harness` 的定义是有意的，改它等于重新定义这一层而不是修正它。
- **20 次运行不足以给四个任务排序**，`pass@k` 仍是 100%（每个任务至少成功过一次）。它确立的是判据不再饱和——这是"在 `hard` 上比较 Context 策略准确性"的前置条件，不是那个实验本身。
- **34 次拦截里 31 次是 `find` 未进 allowlist。** `hard` 与 `repo` 用同一份 `python` 单命令 allowlist，所以两次运行可比；但这也是最小 allowlist 的真实代价，且它的实际约束力有限——`python` 本来就能做那些被拒命令能做的事。

## [0.3.1] - 2026-09-21

**Context 协议修复。** 第一次真实模型的 Context 消融给出了一组"看起来很像结论"的数字：六个变体里四个成功率只有 0–22%，而全部失败都被记成 `provider_error`。查下去发现那不是模型能力，而是 harness 缺陷——截断型 Context 策略会把一次工具调用回合从中间切开，provider 因此返回 400。**这个缺陷在 scripted 模型下结构性不可见**：脚本模型不校验它收到的请求，所以 deterministic 阶段无论跑多少遍都照不出来；换成真实 provider 后它表现为一个不透明的 400，并且被失败分类归到 `environment` 层，与"provider 挂了"无法区分。本节修掉这个缺陷、让下一次 400 能被诊断，并交付修复后重跑的消融结果。

### Added

**真实模型消融结果**

- **Context 策略消融**（6 变体 × 3 任务 × 3 次重复 = 54 次运行，全部通过）：归档在 [`docs/results/stress-real-context-ablation.md`](docs/results/stress-real-context-ablation.md)。`repo_retrieval` 是成本赢家（每次成功 **$0.001243**，唯一在成本轴上不被支配的变体），步数也接近最少（6.56）。
- **核心读数：Token 更少 ≠ 更便宜，在缓存计价下会反号。** `sliding_window`（26,096 Token）与 `summary_compaction`（26,073）都比 `full_history`（29,315）省约 11% Token，**却都更贵**（$0.001398 / $0.001480 vs $0.001284）。机制是 prompt cache：命中价 $0.003 与未命中价 $0.15 相差 **50 倍**，而截断策略每一步都在改动前缀、把可复用前缀打断；`full_history` 只追加不修改，因此拿到最高的 92% 命中率。**这是"用 Token 数当成本代理"会系统性出错的地方**，也是只有真实 provider 才能测到的东西。
- **工具输出消融**（3 变体 × 3 任务 × 3 次重复 = 27 次运行，全部通过）：归档在 [`docs/results/stress-real-tool_output-ablation.md`](docs/results/stress-real-tool_output-ablation.md)。`bounded_output_artifact` 在 Token / 成本 / 步数三轴都不被支配——相比不设上限省 6.6% Token、5.9% 成本而成功率不变；**单把上限从 200K 压到 20K 只省 2.1%，真正的杠杆是溢出落盘而不是上限**。
- `summary.json` 新增 `real_model_ablations` 块；README 新增"真实模型消融"一节，两组表格都放在 `<!-- real-model-ablation-results:start -->` 标记内；[`docs/real-model-evaluation.md`](docs/real-model-evaluation.md) 新增第 10 节，分列两组结果、这次修复的发现路径，以及"这两组实验不成立什么"；[`docs/context-engineering.md`](docs/context-engineering.md) §4 补上真实模型读数与缓存反号结论。

### Fixed

- **截断型 Context 策略会把工具调用回合切成两半。** OpenAI 兼容协议要求 `tool` 消息前面必须有声明其 `tool_call_id` 的 assistant 消息；`sliding_window` / `summary_compaction`（以及基于后者的 `retrieval_summary` / `symbol_retrieval`）按裸下标切片，可能保留工具结果却丢掉声明它的那一轮 assistant，provider 直接返回 400。新增 `_window_start()`：窗口起点向前回退到开启该回合的 assistant 消息，但不越过钉住的 system/task 前缀；回退量记入 `window_extended_for_tool_round`，让这次加宽可观测而不是静默发生。`summary_compaction` 顺带修掉一个相关问题：没有可安全压缩的内容时不再插入一条空摘要消息。
- **provider 的错误响应体被直接丢弃，导致 400 无法诊断。** `_http_error` 只保留状态码，而 400 的成因只存在于响应体里——于是"我们把请求拼错了"和"provider 出故障了"留下完全相同的记录，前者还会被永久归因给后者。现在读取、折叠、截断（400 字符）并脱敏响应体，附在四类 HTTP 失败之后。这是把请求构造失败从 provider 故障里分出来的第一步。
- **`uv.lock` 里的项目版本停留在 `0.2.0`。** `0.3.0` 发布时漏改了锁文件中的自身版本，`uv sync` 会把它改回来，于是每次同步都产生一个无关的 diff。

### Changed

- **脱敏规则收敛为单一实现。** 新增 `minidsh.domain.redaction`（`redact_text` / `redact_value` / `SECRET_KEYS` / `REDACTED`），Trace 写入器与模型 adapter 共用同一套规则，删掉 Trace 写入器里的私有副本。错误响应体是最容易意外泄露凭据的路径——它是不可信文本，却要直接进入日志、Trace 与 `failure_reason`。

### Tests

- 203 个测试（+9）：`tests/test_context.py` 新增四个，用含真实工具调用回合的历史断言"任何策略保留的窗口都不以孤儿 `tool` 消息开头、且 assistant 声明的每个调用都有结果应答"，覆盖两个截断策略、检索包装器与 `floor` 边界；`tests/test_integration.py` 新增三个，覆盖错误响应体进入失败原因、其中凭据被脱敏而诊断信息保留、以及响应体被截断；`tests/test_docs.py` 新增两个，把 README 两组消融表**逐格**与 `summary.json` 的 `real_model_ablations` 比对（按列名而不是列位置取数，因此重排表格不会把数字配错标签），并逐份校验模型名、thinking 开关与费率。
- 九个门禁全部通过"故意改坏、确认失败、再改回"验证：其中消融数字门禁分别用改 Token、改缓存占比、**交换两列**、删掉费率四种破坏方式实测，四种都被拦下。
- **修复对 scripted 结果无影响，且已逐字段验证。** 重跑全部 scripted 基线与消融后与归档比对：成功率、步数、Token、成本、拦截数完全一致，只有受机器负载影响的墙钟延迟不同。Token 一致即说明送进模型的 prompt 一致——脚本历史里没有窗口会切断工具回合。因此 `docs/results/` 的脚本化部分无需改动，这本身就是"修复行为保持"的证据。

### 说明

- **两组消融的成功率都是 100%，因此都只能得出成本结论，不能得出"某策略更准"的结论。** `stress` 套件对 `deepseek-flash` 太容易，六个变体 54 次运行无一失败。要比较准确性需要更硬的任务集——这是下一步的前置条件，不是本次实验的精细化。
- 修复前那版 Context 消融（0–22%）**不作为 Context 结论**，它测量的是上述 harness 缺陷；原始文件已不在归档中。

## [0.3.0] - 2026-09-21

**Real-model results release。** `0.2.0` 补上了"在真实模型上做实验"所需的全部工程，但它的报告里没有一个真实模型的数字；`0.3.0` 交付第一批实测结果，并修掉接入过程中暴露的 provider 缺陷。`0.2.0` 的条目保持发布时的原样，修正集中记在本节。

### Added

**真实模型实测结果**

- 首次在真实 provider 上跑通 `repo` 套件：6 个跨文件任务 × 3 次逐任务交错重复 = **18 次运行全部通过**，`pass@k` 100%，成功率极差 0.00，失败层分解 `success 18 / harness 0 / agent 0 / environment 0`。报告归档在 [`docs/results/repo-real.md`](docs/results/repo-real.md)，机器可读汇总进入 `docs/results/summary.json` 的 `real_model` 块。
- 实测读数（`deepseek-flash`，thinking 关闭，`temperature=0`，off-peak 费率 $0.15 / $0.6 / $0.003 per 1M tokens）：平均 6.61 步、19,132 Token、10.28 次工具调用；**缓存输入占比 86%**；平均延迟 6,410.1 ms = model 6,290.6 + tool 103.1 + **harness 16.4（占墙钟 0.26%）**；总成本 **$0.016865**，**每次成功成本 $0.000937**。
- **报告现在记录自己的实验条件。** `BenchmarkReport` 新增 `model` 块，写入 profile / endpoint / 上下文窗口 / thinking 开关 / `temperature_effective` / 显式费率，Markdown 报告新增 "Model and rate card" 章节。此前这些事实只存在于产生报告的那条 shell 命令里，一份成本报告脱离命令行历史就无法审计。
- `scripts/archive-results.py` 归档真实模型报告并在 `summary.json` 中生成 `real_model` 块。费率必须由环境变量显式声明：缺失时保留上一份而不是写入未知费率，因为 off-peak 是 peak 的一半，同一份报告有两组都正确的成本数字。
- 文档：README 新增"真实模型基线"一节，含"这个 100% 只说明这么多"的四条限制；[`docs/real-model-evaluation.md`](docs/real-model-evaluation.md) 新增第 9 节，分别记录这次实测**成立**与**不成立**的结论；`docs/resume.md`、`docs/technical-highlights.md` 同步。

### Changed

- README 的真实模型示例命令改用实际费率，不再是 `<in>` / `<out>` 占位符，并写明"归档要在重跑之前做"——报告与 Trace 是原地覆盖的。

### Fixed

- **DeepSeek profile 与官方文档不符。** 实测官方 API 后修正三处：endpoint 应为 `https://api.deepseek.com/chat/completions`（`0.2.0` 记录的 `/v1/chat/completions` 可通，但不是文档给出的形式）；`deepseek-chat` / `deepseek-reasoner` 已不在官方模型列表内，改为 `deepseek-flash` 与 `deepseek-v4-pro`；上下文窗口由 128K 改为官方公布的 1M。
- **thinking 模式（上游默认开启）会让 harness 在第一次工具调用后收到 400。** 官方文档明确：请求带 `tools` 时，历史每一轮的 `reasoning_content` 必须原样回传，否则 API 返回 400。原实现既不解析也不回传该字段，因此默认配置下多步工具循环必然失败——这是换成真实 provider 才暴露的第一个整体性缺陷。现在 `Message` / `ModelResponse` 携带 `reasoning_content`，adapter 解析并在带 `tools` 的请求中回传。
- **thinking 模式会静默忽略 `temperature`**，所以开着它 `--temperature 0` 等于没设——重复采样协议赖以成立的确定性假设会被悄悄破坏。harness 现在默认关闭 thinking，`--thinking/--no-thinking` 可显式切换，并在 `describe()` 里记录 `temperature_effective`，让报告自己说清属于哪种情况。
- **`mini-dsh report` 从不显示模型信息。** 渲染器读取 `model` 键，但 `BenchmarkReport.to_json()` 从来没有写过它：一个只存在于阅读路径、在写入路径上缺失的字段，而消融报告早已写入同一键。现在基准报告也写入该键，缺失时省略而不是写 `null`——"未记录"与"没有模型"不是同一个断言。
- **逐次运行标签与报告级条件可能互相矛盾。** `BenchmarkRunner` 的 `model_label` 与报告条件描述同一个模型却分别传入，两者可以不一致且很难被发现。合并为单个 `model` 参数，标签由它派生。

### Tests

- 194 个测试（+8）：`tests/test_docs.py` 新增两个门禁，把 README 上**需要花钱才能产生**的真实模型数字（成功率、`pass@k`、平均步数 / Token、缓存占比、三层延迟、总成本、每次成功成本、拦截数）与 `docs/results/summary.json` 的归档值逐项比对，并单独校验 README 引用的模型名、thinking 开关与费率与归档一致——费率断言用 `$` 锚定，避免"6,290.6 里含有 0.6"这类假通过；`tests/test_benchmark.py` 新增两个测试，覆盖报告写入条件块、以及无条件时省略该键而非写 `null`。

## [0.2.0] - 2026-09-21

**Real-model evaluation release。** `0.1.0` 证明了 Harness 按规格工作；`0.2.0` 补上"在真实模型上做实验"所需的全部工程：可复现的 provider 接入、重复采样、成本与延迟分解、失败分类、Trace 回放与差分、以及报告里的 Pareto trade-off。

> 本节记录发布时的内容。DeepSeek profile 的三处事实修正、`reasoning_content` 回传与 thinking 开关见 [`0.3.0`](#030---2026-09-21)。

### Added

**真实模型接入**

- `ModelProfile` 与四个内置 profile：`deepseek`、`deepseek-reasoner`、`openai`、`ollama`，声明 endpoint / model / 凭据变量 / 上下文窗口，支持 `MINIDSH_MODEL_PROFILE` 与逐字段 `--endpoint` / `--model` / `--api-key-env` 覆盖。
- **价目不内置**：真实 provider 的 `pricing` 为 `None`，未声明价目时拒绝运行（而不是报告 `$0`）。零价目与未声明价目不可区分，因此"是否已声明"被显式跟踪；`--allow-unpriced` 是显式逃生口。
- `OpenAICompatibleModel` 强化：429/5xx/超时/网络错误有限重试与指数退避（上限可配，`sleep` 可注入以便测试）；HTTP 失败分类为 `auth_error` / `rate_limited` / `server_error` / `client_error`；`ModelAdapterError` 带 `code` 与 `retryable`，失败分类不再依赖解析错误文本。
- Prompt cache 支持：解析 `usage.prompt_tokens_details.cached_tokens`，`TokenUsage` 新增 `cached_input_tokens` 与 `billable_input_tokens`。

**评测层（新包 `minidsh/evaluation/`）**

- `tracefile.py`：JSONL Trace 的类型化只读视图，按 step 分组，导出状态、失败原因、Token 与成本。Trace 是唯一事实源，其余全部是可再生成的视图。
- `taxonomy.py`：9 个失败类别归并成 4 层（`success` / `harness` / `agent` / `environment`），带证据链的 `FailureDiagnosis`。决策顺序先测无歧义的结构化事实，再对模型做推断；"被绕过的拒绝不算病因"与"`context_miss` 需要两半证据"是两条关键规则。
- `latency.py`：`model_ms` / `tool_ms` / `harness_ms` 三层分解与按工具的延迟聚合。
- `pareto.py`：Success Rate vs Tokens / Cost / Steps 的 Pareto 前沿、被支配变体及其支配者与差值；全并列时明确报告"这一轴无法区分"，而不是伪装成 trade-off。
- `replay.py`：不重新调用模型的逐步回放，按 Loop 自身词汇输出 `Context → Model → Tool Call → Observation → State`。
- `diff.py`：对齐两条 Trace 的逐步差分，定位第一个行为分叉点，给出 Context 差、Token 差与工具调用分叉原因。

**Benchmark**

- `BenchmarkRunner` 支持 `--repeat N`（逐任务交错采样，避免 provider 抖动在整个套件上相关）与 `model_label` 覆盖；`TaskMetrics` 新增 `run_index`、`cached_input_tokens`、`failure_category` / `failure_layer` / `failure_reason` 与三项延迟分量。
- 聚合指标新增 `runs`、`pass_at_k`、`cost_per_success_usd`、`cached_input_share`、三项平均延迟与 `repeat_variance`（`success_spread` / `token_spread`）。
- 报告新增 "Outcome by layer" 层分解表与失败类别 / 原始失败类型两张分布表。
- `AblationReport` 新增 `points()` / `pareto()`，Markdown 追加完整的 Pareto trade-off 章节，JSON 内嵌 `pareto` 结论。
- 新增 `repo` 套件（6 个任务，无 response script，每个跨至少两个模块）：跨文件 Bug Fix、定义+调用点契约、测试失败定位、新功能+测试、配置修改、多轮错误恢复。生成脚本 `benchmarks/build_repo.py` 固定每个 fixture 的 `sha256:` 摘要。
- `JsonlTraceWriter` 新增 `truncate_on_start`：writer 自己拥有文件生命周期，调用方不再需要先删除旧 Trace。

**CLI**

- `benchmark` / `ablation` 新增 `--provider` / `--model-profile` / `--model` / `--endpoint` / `--api-key-env` / `--temperature` / `--max-attempts` / `--repeat` / `--allow-unpriced` / `--cached-input-usd-per-million`。
- `mini-dsh report <json>`：离线重渲染已保存报告并附 Pareto 分析，不重新调用模型。
- `mini-dsh trace replay <jsonl>` 与 `mini-dsh trace diff <a> <b>`，均支持 `--json`。

**文档**

- 新增 [`docs/real-model-evaluation.md`](docs/real-model-evaluation.md)：profile 表、成本模型、延迟模型、重复采样协议、套件选型、结果读法，以及 `3.38×` / `+40%` / `−27%` 三个数字的 workload / baseline / variant / metric / run count / 是否真实模型六项来源说明。
- 新增 [`docs/failure-taxonomy.md`](docs/failure-taxonomy.md)：类别表、层归属、决策顺序、诊断证据格式、Trace 回放与差分工作流、失败案例分析流程。

### Changed

- `ModelPricing` 新增 `cached_input_usd_per_million`；未声明时回落到未缓存费率，**与旧成本模型逐位等价**（`0.1.0` 的全部成本读数不变）。
- `AgentLoop` 在 `model_call_completed` 中发布 `duration_ms` 与 `cached_input_tokens`，`run_finished` 中发布 `latency` 分解；`RunResult` 新增 `latency` 字段。
- `BenchmarkRunner` 不再删除旧 Trace 文件，改为由 writer 截断；Trace 路径在 `--repeat > 1` 时为 `<task>.run<N>.jsonl`（第 0 次仍为 `<task>.jsonl`，保持既有产物路径）。
- 配置层新增 `MINIDSH_CACHED_INPUT_USD_PER_MILLION`。
- 环境变量 `--api-key-env` 默认值由固定字符串改为"沿用 profile 声明的变量"。

### Fixed

- 真实 provider 的 HTTP 失败此前统一抛出无结构的 `ModelAdapterError`，且 5xx/429 不会重试；现在按状态码分类并只对瞬时错误重试。
- 旧 Trace 未被清理时会与新运行的事件交错；现在由 writer 在首次写入时截断。
- **失败的模型调用其耗时此前不计入任何一层**，于是被算进 `harness_ms` 残差——provider 越慢越像"harness 慢"，正好与延迟分解的目的相反。修复覆盖两处：Loop 现在把失败与被取消的调用也计入 `model_ms` / `model_calls`；`latency_from_trace` 改为优先采用 `run_finished` 上由 Loop 记录的权威分解，只在旧 Trace 缺该字段时才回退到按事件求和（失败的调用没有 `model_call_completed` 事件，按事件求和必然丢时间）。这个缺陷是在用本地 `ollama` profile 做端到端探测时暴露的：连接失败的 3.5 秒被全部记到了 harness 头上。

- **`.env.example` 的说明与运行时行为不符**：文件头写"Copy to .env"，但运行时从不读取 `.env`（没有 dotenv 依赖，也没有任何代码加载它），而 `.gitignore` 又特意为 `.env` 开了豁免——三者互相矛盾。现已改成诚实的说明：这是一份**变量名对照表**，不是会被自动加载的文件，并给出可用配方 `set -a; source .env; set +a`。同时写明凭据**不接受**来自 `minidsh.toml`（该文件会被提交与共享，密钥写进去就是等着泄露；写进去会被直接拒绝，而不是被静默忽略）。
- `MINIDSH_API_KEY_ENV`（"我的密钥在哪个变量里"）此前只对命名 profile 生效，在 `--endpoint` 直连路径上被静默忽略——同一件事的含义取决于用了哪个 flag。现在两条路径一致，优先级为 `--api-key-env` > `MINIDSH_API_KEY_ENV` > 默认 `MINIDSH_API_KEY`。

### Tests

- 186 个测试（+55）：新增 `tests/test_evaluation.py`（29 个）覆盖 Trace 解析、失败分类的每一条分支与决策优先级、延迟分解、Pareto 前沿与并列、回放与差分；`tests/test_docs.py`（9 个）把 README 与 `docs/` 上的版本、模块数、任务数与测试数变成**由仓库推导的断言**，任一数字手改失配即门禁失败；`tests/test_agent_loop.py` 新增"失败的模型调用必须记在模型账上而非 harness 账上"的回归测试；`tests/test_integration.py` 新增 429→503→200 重试成功、401 不重试、prompt cache 解析；`tests/test_benchmark.py` 新增重复采样与层归属；`tests/test_cli.py` 新增 report / replay / diff 与 profile 拒绝路径。

## [0.1.0] - 2026-09-21

首个可用版本：一个可替换、可观测、可复现的 Coding Agent Harness，附带可执行的消融实验协议。

### Added

**核心 Runtime**

- 完整的 `Observe → Plan → Act → Execute → Observe → Continue | Finish` Agent Loop，含步数、Token、工具调用与墙钟四层预算，终态限定为 `finished` / `failed` / `cancelled` / `budget_exhausted`。
- 端口驱动的依赖注入：`ModelPort` / `ContextPort` / `ToolExecutorPort` / `SessionPort` / `EventSink`，全部在 `composition.py` 一处装配。
- 插件 Runtime：稳定 ID、版本、`provides` / `requires` 声明、重复服务与依赖环检测、启动失败逆序回滚、步骤边界热替换。
- 版本化 Event Bus，订阅者失败不影响核心状态一致性。

**Context Engineering**

- 六种共用同一接口的策略：`full_history`、`sliding_window`、`summary_compaction`、`repo_retrieval`、`retrieval_summary`、`symbol_retrieval`。
- `ContextView` 自报策略名、版本、Token 估算、丢弃消息数与元数据；原始历史只读，摘要与检索均为可再生成的派生视图。
- 两个纯本地确定性检索器：词面重叠 `RepositoryRetriever` 与基于 AST 的 `PythonSymbolRetriever`，无需向量库。

**Tool Runtime**

- 统一流水线 `Schema → Validation → Authorization → Execution → Observation`。
- Read / Write / Patch / Search / Shell / Git / Test / Build 工具；Patch 优先于整文件覆盖；写入使用原子替换。
- 工具输出大小上限（默认 20 000 字符）、显式截断标记与 `LocalArtifactStore` 落盘。
- 结构化 Observation：状态、结果或错误、耗时、截断信息、可重试性；被拒绝的调用同样发布事件。

**安全模型**

- `WorkspaceSandbox`：拒绝绝对路径、真实路径归属校验、符号链接防逃逸、不展开 `~`、原子写入校验父目录。
- `CommandPolicy`：deny-first（`rm` / `sudo` / `ssh` / `scp`）、显式 allowlist、argv 数组不经过 Shell、空 argv 拒绝。
- `NetworkPolicy`：默认 deny-all，按 `(host, protocol, port)` 三元组授权。
- 密钥文件拒绝（`.env` / `.pem` / `.key` / `.p12` / `.ssh` / `id_rsa` 等），错误消息不回显内容。
- 子进程环境变量 allowlist；超时后 `killpg` 清理整个进程组。
- Trace 落盘前对 Secret 键、`Bearer` 头与 `key=value` 赋值二次脱敏。
- 可选 `DockerCommandRunner`：只读根文件系统、`cap-drop ALL`、`no-new-privileges`、PID/内存/CPU 上限、`/tmp` noexec、默认 `--network none`。

**配置**

- 分层优先级 `CLI > MINIDSH_* 环境变量 > minidsh.toml > 安全默认值`，可打印每个字段的来源。
- 类型转换失败显式报错，未知或越界字段被拒绝；`max_seconds` 作为 `max_duration_seconds` 的兼容别名。

**Benchmark 与消融**

- 版本化 manifest（`schema_version` / `repo_commit` / `seed` / `model` / `budget` / `isolation` / `success_argv`），路径相对 manifest 解析。
- 隔离任务副本、独立进程执行可执行判据、JSONL 结果与逐任务 Trace 持久化。
- 指标：成功率、步数、Token、工具调用、延迟、成本、重试成功率、权限拒绝数、危险动作拦截数、失败类型。
- `AblationRunner` 与四组内置 suite：`standard`、`context`、`tool_output`、`permission`，单变量对照。
- 29 个任务：`smoke`（1）、`core`（25，覆盖六类任务）、`stress`（3，长历史 / 大输出 / 被拒绝动作）。

**模型适配器**

- `ScriptedModel`：确定性响应序列，未声明 Token 时测量实际上下文，支持从文件加载。
- `OpenAICompatibleModel`：只从显式配置读取凭据，含工具 Schema 传递与非法参数处理。

**工程与文档**

- `ruff format` / `ruff check` / `mypy --strict` / `pytest` 四项门禁，CI 覆盖；131 个测试分 `unit` / `integration` / `e2e` 三层，覆盖率 83%。
- `runtime/` 覆盖率为 100%：插件重复 ID、重复服务提供者、依赖环、声明未注册、启动回滚、回滚失败、聚合关停失败、热替换三类失败路径全部有测试守住。
- 最小端到端示例 `examples/minimal_demo.py` 与一键脚本 `scripts/run-{benchmark,ablation,all-experiments}.sh`。
- 文档：README（架构图、Loop 流程图、Benchmark 方法、真实结果表）、`docs/benchmark.md`、`docs/context-engineering.md`、`docs/sandbox-security.md`、`docs/technical-highlights.md`、6 条 ADR。

### Measured results

环境：`scripted` 模型、seed `0`、$3 / $15 per million tokens。

| 实验 | 读数 |
| --- | --- |
| `smoke` 基线 | 1 任务 / 100% / 208.0 Token / $0.001080 |
| `core` 基线 | 25 任务 / 100% / 749.9 Token / $0.064545 |
| `stress` 基线 | 3 任务 / 100% / 9534.0 Token / $0.095022 / denied=1 / blocked=4 |
| Context 消融（stress） | `sliding_window` 8981.0 vs `repo_retrieval` 12617.0 Token（+40%） |
| 输出管理消融（stress） | `unbounded` 32136.0 vs `bounded` 9511.3 Token（3.38×） |
| 权限消融（stress） | `basic_feedback` 8622.7 vs `detailed_feedback` 9534.0 Token |

同一命令重复运行得到逐字节相同的 Token 与成本读数：`AgentLoop` 刻意不把墙钟 `duration_ms` 写入模型可见的反馈（它只进事件流），否则变长浮点会让 Token 计数在运行间漂移。

### Known limitations

- `LocalCommandRunner` 是策略层而非容器/内核级隔离；不可信模型与仓库必须使用 Docker 后端或外层 VM。
- 脚本化模型下成功率恒为 100%，因此当前实验只能得出成本与行为结论，不能得出准确率结论。
- `symbol_retrieval` 目前只支持 Python。
- 单 Agent、单工作区、单进程；多 Agent 与分布式执行不在核心层范围内。

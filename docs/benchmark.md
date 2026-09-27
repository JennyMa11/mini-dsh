# Benchmark 与消融实验

Mini-dsh 的 Benchmark 层解决一个问题：**在模型不变的条件下，量化 Harness 设计（Context 策略、工具输出管理、权限反馈、重试）对成本与成功率的影响**。因此它把"可复现"当作硬约束——任务输入、预算、隔离配置和成功判据全部固定并入库，成功与否由可执行判据判定，而不是模型自报。

## 1. Manifest 格式

Manifest 是 JSON，必须包含 `schema_version` 与 `tasks`。所有相对路径以 manifest 所在目录为基准解析。

```json
{
  "name": "mini-dsh-stress",
  "schema_version": "2.0",
  "tasks": [
    {
      "id": "stress-large-output",
      "goal": "Read big.log, record how many lines it contains in summary.py, then run the tests.",
      "workspace": "fixtures/stress-large-output",
      "response_script": "responses/stress-large-output.json",
      "test_argv": ["python", "-m", "unittest", "discover", "-s", "."],
      "success_argv": ["python", "verify.py"],
      "timeout_seconds": 120,
      "tags": ["large-output", "stress"],
      "category": "large-output",
      "repo_commit": "sha256:48a2af4428a9999f",
      "seed": 0,
      "model": "scripted",
      "budget": {
        "max_steps": 12,
        "max_tokens": 400000,
        "max_tool_calls": 20,
        "max_duration_seconds": 300
      },
      "isolation": {
        "sandbox_backend": "local",
        "allow_write": true,
        "allow_shell": true,
        "allowed_commands": ["python"],
        "denied_tools": [],
        "network_rules": []
      }
    }
  ]
}
```

### 字段含义

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 任务唯一标识，用于报告、Trace 文件名与去重 |
| `goal` | 是 | 交给 Agent 的自然语言目标 |
| `workspace` | 是 | 初始仓库副本，Runner 会整体复制到临时目录 |
| `success_argv` | 是 | **成功判据**，argv 数组，退出码 0 记为成功 |
| `response_script` | 否 | 脚本化模型响应文件；省略则需真实 provider |
| `test_argv` | 否 | 暴露给 Agent 的测试命令（注册为 `run_tests` 工具） |
| `build_argv` | 否 | 暴露给 Agent 的构建命令 |
| `timeout_seconds` | 否 | 判据执行超时，默认 120s |
| `tags` / `category` | 否 | 分组与报告维度 |
| `repo_commit` | 否 | 输入版本钉扎（仓库内任务用 fixture 内容摘要，SWE-bench 类任务用真实 commit） |
| `verification_overlay` | 否 | **留出判据目录**，相对 manifest 解析。Agent 结束后、`success_argv` 执行前覆盖进工作区；声明了却指不到目录会被直接拒绝 |
| `verification_commit` | 否 | overlay 内容摘要，钉住留出判据的版本，语义同 `repo_commit` |
| `seed` | 否 | 采样种子，默认 0 |
| `model` | 否 | 模型标识，默认 `scripted` |
| `budget` | 否 | `max_steps` / `max_tokens` / `max_tool_calls` / `max_duration_seconds` |
| `isolation` | 否 | 沙箱后端、写/Shell 开关、命令 allowlist、工具 denylist、网络规则 |

`isolation` 会被逐任务钉扎并原样写入报告，因此任何一次实验的权限面都是可审计的——不会出现"两次运行权限不同却比较指标"的情况。

### `verification_overlay`：让判据不再是 Agent 能改的那份文件

默认的判据是 `success_argv` 在工作区里跑测试——而工作区正是 Agent 能读、能改的地方。于是成功率测的是"是否让可见测试变绿"。`verification_overlay` 换掉了这份判据：

```text
copy workspace → temp dir
run Agent Loop
├─ copy verification_overlay → workspace     # 此刻起判据才存在
└─ run success_argv                          # 判据 = Agent 从未见过的文件
```

**时机是全部关键**：覆盖早了判据可读可改；覆盖晚了判据不参与判定。overlay 里应当只放判据文件——放进 Agent 本该产出的文件，它会被静默覆盖。

## 2. 执行模型

```text
manifest.json
   │  validate (pydantic, extra="forbid")
   ▼
for each task:
   ├─ copy workspace → temp dir          # 任务间零污染
   ├─ build runtime from isolation        # 权限面按任务重建
   ├─ run Agent Loop until terminal state
   ├─ copy verification_overlay (if any)  # 留出判据在此刻落地
   ├─ run success_argv in the temp dir    # 独立进程，不信模型自报
   ├─ collect metrics from event stream
   └─ persist traces/<task_id>.jsonl
   ▼
aggregate → report.md / report.json / results.jsonl
```

Runner 为每个任务复制临时工作区，Agent 完成后**独立执行**成功判据。判据退出码为 0 才算成功；非 0 记为 `verification_failed`，超时记为 `verification_timeout`，被权限层拒绝记为 `verification_denied`，overlay 无法落地（例如 Agent 在 overlay 期望目录的位置留了一个文件）记为 `verification_overlay_failed`——只让该任务失败，不中断整轮扫描。

## 3. 指标定义

| 指标 | 定义 |
| --- | --- |
| `success_rate` | 成功任务数 / 任务总数，成功 = 判据退出码 0 |
| `average_steps` | 每个任务的平均 `step_id` 终值 |
| `average_tokens` | 平均输入+输出 Token，由 Harness 侧确定性估算 |
| `average_tool_calls` | 平均工具调用次数（含被拒绝的调用） |
| `average_latency_ms` | 平均端到端墙钟延迟 |
| `total_cost_usd` | Σ 每任务成本，成本 = `input_tokens × 输入单价 + output_tokens × 输出单价` |
| `retry_success_rate` | 重试后成功的比例（`retry_successes / retries`） |
| `permission_denied` | 被 PermissionPolicy 拒绝的工具调用数 |
| `unsafe_blocked` | 被命令策略或沙箱判据拦截的危险动作数（`command_denied` + `sandbox_violation`） |
| `failure_type` | `none` / `verification_failed` / `verification_timeout` / `verification_denied` / `verification_overlay_failed` / 运行终态 |

指标同时按 `category` 与 `failure_type` 聚合，便于定位"哪一类任务变贵了"和"失败集中在哪种模式"。

## 4. 消融协议

`AblationRunner` 对**同一份 manifest** 依次运行一组 `HarnessVariant`，每个变体只改一个变量。

```python
HarnessVariant(
    name="sliding_window",
    context_strategy="sliding",  # 唯一改动
    reflection=False,
    detailed_tool_feedback=True,
    max_retries=0,
    tool_output_limit=20_000,
    artifact_offload=True,
)
```

内置四组 suite：

| Suite | 变体 | 变量 |
| --- | --- | --- |
| `standard` | `base_agent` / `repo_retrieval` / `context_compaction` / `reflection` / `better_tool_feedback` | 累积配置 |
| `context` | `full_history` / `sliding_window` / `summary_compaction` / `repo_retrieval` / `retrieval_summary` / `symbol_retrieval` | Context 策略 |
| `tool_output` | `unbounded_output` / `bounded_output` / `bounded_output_artifact` | 输出上限与 artifact 落盘 |
| `permission` | `no_retry` / `single_retry` / `basic_feedback` / `detailed_feedback` | 重试上限与反馈粒度 |

协议约束：

- **单一变量**：除被研究的变量外，模型、任务集、预算、隔离配置、采样参数与环境必须一致。`base_agent` 变体刻意与 `RuntimeOptions` 默认值逐字段对齐，并有回归测试（`test_base_variant_matches_the_runtime_defaults`）守住这一点，避免"基线其实不是默认配置"。
- **同 manifest**：所有变体跑同一份任务与 fixture；`AblationRunner` 拒绝重复变体名，并记录所用 manifest。
- **可审计**：每个变体产出一份 `AblationReport`，可序列化为 JSON / JSONL / Markdown，与基线使用同一套指标定义。

出于安全原因，本地执行**不会**通过消融实验关闭路径限制；比较 Sandbox 强弱必须使用隔离的 hardened adapter，而不是在本地放宽策略。

## 5. 复现实验

```bash
./scripts/run-all-experiments.sh              # 全部基线与消融
./scripts/run-benchmark.sh core               # 单套基线
./scripts/run-ablation.sh stress context      # 单组消融
```

脚本固定了定价与验证命令，产出 Markdown 报告到 `docs/results/`。等价的手工调用：

```bash
uv run mini-dsh benchmark benchmarks/core/manifest.json \
  --output benchmark-results/core.json \
  --allow-write --allow-shell \
  --verify-command python \
  --input-usd-per-million 3.0 --output-usd-per-million 15.0
```

## 6. 任务集

| Suite | 任务数 | 覆盖 |
| --- | --- | --- |
| `smoke` | 1 | 读 → Patch → 测试 → 回答的端到端链路自检 |
| `core` | 25 | Bug Fix(5) / Feature(4) / Test Repair(4) / Refactor(4) / Code Understanding(4) / Multi-file(4) |
| `stress` | 3 | 长历史(18 步、16 模块)、大输出(75 KB / 3000 行日志)、被拒绝动作(网络+破坏性命令+密钥读取+路径逃逸) |
| `repo` | 6 | 跨文件 Bug Fix、定义+调用点契约、测试失败定位、新功能+测试、配置修改、多轮错误恢复（真实 provider） |
| `hard` | 4 | 模块级规格实现：区间合并、令牌桶、分层配置、RFC 4180 解析（真实 provider，**留出判据**） |

`stress` 是专门为**让消融变量可观测**而设计的：如果任务太小，所有 Context 策略的 Token 几乎相同，实验就没有区分度。

`repo` 与 `hard` 只跑真实 provider（无 response script）。两者的差别在判据：`repo` 的成功判据是 `unittest discover -s .`，跑的是 Agent 能读能改的可见测试；`hard` 的判据来自 overlay，Agent 在运行期间看不到。

`hard` 的公平性是刻意设计的。**每个被留出检查覆盖的契约点都写在模块 docstring 里**，任务目标也明说"`tests/` 只是 smoke 子集，docstring 才是判据"，所以失败意味着"没有实现写明的契约"而不是"被藏了需求"。两份产物由 [`benchmarks/build_hard.py`](../benchmarks/build_hard.py) 生成，并由 `tests/test_hard_suite.py` 在离线、不调模型的前提下双向门禁：

- 出厂 fixture 必须**失败**——否则任务一开始就是解好的，成功率会被一个不需要工作的任务抬高；
- 随包参考解必须**通过**——否则低成功率无法与"任务本身做不了"区分，0% 就成了一个无法解读的数字。

判据脚本 `run_verification.py` 也来自 overlay，它把发现范围钉死在 `verification_tests/` 并断言用例数不低于声明值：`unittest discover` 在空目录上退出码是 0，不设这条下限的话"删掉测试目录"就是最省事的作弊方式。

## 7. 结果解读

当前工作区的免费回归、重复性检查与 Session 日志对照见
[2026-09-27 离线验证报告](results/offline-regression-2026-09-27.md)。该记录与下方历史
归档分别保留，脚本模型的 USD 为模拟折算值，实际没有模型 API 费用。

环境：`scripted` 模型、seed `0`、定价 $3 / $15 per million tokens。完整报告见 [`docs/results/`](results/)，汇总见 [`summary.json`](results/summary.json)。

| 实验 | 关键读数 | 结论 |
| --- | --- | --- |
| 基线 `smoke` | 1 任务 / 100% / 208.0 Token / $0.001080 | 链路自检 |
| 基线 `core` | 25 任务 / 100% / 749.9 Token / $0.064545 | 六类任务全部通过，链路完整 |
| 基线 `stress` | 3 任务 / 100% / 9534.0 Token / $0.095022 / denied=1 / blocked=4 | 权限统计非零，拦截生效 |
| Context 消融 | `sliding_window` 8981.0 vs `repo_retrieval` 12617.0 Token（+40%） | 检索策略把相关文件重新注入，换取信息量、付出 Token |
| 输出管理消融 | `unbounded` 32136.0 vs `bounded` 9511.3 Token（3.38×） | 输出上限是最大的单点成本杠杆 |
| 权限消融 | `basic_feedback` 8622.7 vs `detailed_feedback` 9534.0 Token | 更详细的失败反馈更贵；是否正确率更高需真实模型才能判断 |
| 对照组：`core` 上的 `tool_output` | 三个变体同为 749.9 Token | 该套件无大输出任务，此对照在 `core` 上不可分辨 |

**确定性**：同一命令重复运行得到逐字节相同的 Token 与成本读数。这要求送入模型的上下文必须是确定性的——任何墙钟时间、临时路径或随机数一旦进入 Prompt，Token 计数就会漂移。`AgentLoop` 因此刻意不把 `duration_ms` 写入模型可见的反馈（它只进事件流），并有回归测试守住这一点。

**必须说明的边界**：脚本化模型让成功率恒为 100%，因此 `success_rate` 这一列校验的是 Harness 完整性（Patch 生效、判据可执行、状态机不误判、拦截被记录），**不是模型能力**。任何"某策略更聪明"的结论都必须换成真实 provider 并接受成功率波动后才能下。Token / 成本 / 拦截计数这些与模型无关的读数，在当前设置下是可信的。

## 8. 新增任务的流程

默认流程（判据 = 工作区里的可见测试）：

1. 在 `benchmarks/<suite>/fixtures/<task-id>/` 放入初始仓库（含刻意损坏或缺失的实现）。
2. 写 `verify.py` 作为独立判据，**只依赖标准库**，退出码 0 表示通过。
3. 生成或手写 `responses/<task-id>.json`：省略 `input_tokens`，让 Harness 测量真实上下文。
4. 在 `manifest.json` 中登记任务，补齐 `repo_commit`（内容摘要）、`budget` 与 `isolation`。
5. 跑一次确认成功，再跑 `--suite` 确认消融有区分度（不同变体 Token 应可分辨）。

要测**能力**而不是**顺从可见测试**，改用留出判据（`hard` 套件的做法）：

1. 在 `benchmarks/<suite>/fixtures/<task-id>/` 放工作区，**规格写在模块 docstring 里**，`tests/` 只放 smoke 子集。
2. 在 `benchmarks/<suite>/verification/<task-id>/` 放判据：`verification_tests/` + 一个 `run_verification.py`，后者断言收集到的用例数不低于声明值。
3. 在 `benchmarks/<suite>/solutions/<task-id>/` 放参考解，并用一个离线门禁双向验证：出厂 fixture 必须失败、参考解必须通过。
4. manifest 里补 `verification_overlay`、`verification_commit`，`success_argv` 指向 overlay 里的脚本。
5. **不要**把 Agent 本该产出的文件放进 overlay——它会被静默覆盖。

## 完整离线复现入口

`uv run python scripts/offline_regression.py` 执行 baseline、消融、Session 配对和
Trace 审计。固定提交、锁文件安装、运行条件与报告归档见
[可复现基线](reproducible-baseline.md)。

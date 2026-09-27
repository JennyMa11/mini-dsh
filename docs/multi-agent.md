# Multi-Agent / Sub Agent 协作

Mini-dsh 支持可选的单进程协作：主 Agent 通过 `delegate_sub_agents` 委派多个子任务，独立的 Sub Agent 分析、检索或审查工作区，主 Agent 收集结果并决定后续操作。协作能力由 Harness 实现，沿用已有的模型 Tool Call 接口。

## 启用

```bash
uv run mini-dsh chat --model-profile deepseek --multi-agent
uv run mini-dsh run "委派分析和审查，汇总后修复问题" \
  --model-profile deepseek --multi-agent --allow-write
uv run mini-dsh gui --model-profile deepseek --multi-agent
```

启用后由主模型决定是否委派；没有委派 Tool Call 时仍按普通主 Agent 运行。主模型和子模型都需要支持当前 adapter 的工具调用接口。CLI 默认给每个 Sub Agent 构造同一 provider/model 配置的独立 adapter；Python API 可注入其他模型工厂及单独的价格表。

命名方案示例位于 `examples/profiles.toml` 的 `team`：

```toml
[profiles.team]
multi_agent_enabled = true
subagent_max_agents = 4
subagent_max_parallel = 2
subagent_max_steps = 8
subagent_max_tokens = 10000
subagent_max_tool_calls = 16
subagent_max_seconds = 60
enabled_tools = ["read_file", "search", "git", "delegate_sub_agents"]
```

```bash
uv run mini-dsh chat --config examples/profiles.toml --profile team --model-profile deepseek
uv run mini-dsh config --config examples/profiles.toml --profile team
```

如果配置了 `enabled_tools`，须将 `delegate_sub_agents` 放入列表。`--no-multi-agent` 可以撤销低优先级配置中的协作设置，CLI 会从组装用的工具选择中移除委派工具，`--deny-tool delegate_sub_agents` 则保留工具定义并拒绝委派。

| 配置 | CLI | 默认值 |
| --- | --- | --- |
| `multi_agent_enabled` | `--[no-]multi-agent` | false |
| `subagent_max_agents` | `--subagent-max-agents` | 4，单次主运行的累计子 Agent 上限，最多 32 |
| `subagent_max_parallel` | `--subagent-max-parallel` | 2，最多 8 |
| `subagent_max_steps` | `--subagent-max-steps` | 8 |
| `subagent_max_tokens` | `--subagent-max-tokens` | 10000 |
| `subagent_max_tool_calls` | `--subagent-max-tool-calls` | 16 |
| `subagent_max_seconds` | `--subagent-max-seconds` | 60 |

环境变量为对应大写字段加 `MINIDSH_` 前缀，如 `MINIDSH_MULTI_AGENT_ENABLED=true`。

## 委派契约

主模型发出以下 Tool Call；它仍须经过 Schema 校验与 `agent.delegate` 权限判断，再进入调度器。

```json
{
  "name": "delegate_sub_agents",
  "arguments": {
    "tasks": [
      {"name": "analyst", "role": "code analyst", "task": "分析实现与调用关系，给出修改建议"},
      {"name": "reviewer", "role": "code reviewer", "task": "检查边界情况与潜在回归"}
    ]
  }
}
```

名字须为以字母开头的 `snake_case` 标识，单批内不可重复。`role` 默认为 `analyst`，只改变提示词，不授予额外权限。子 Agent 继承系统提示词，再追加角色和只读协作要求；它只收到明确的子任务和自己的历史，不复制主 Agent 整段对话。

结果在同一次工具 Observation 中返回，按输入顺序排列，每项包含名字、角色、独立 `run_id`、终态、回答、错误原因、Token、成本、步骤和工具调用数。启动失败也返回结构化失败；主 Agent 可根据结果继续处理。输出经过既有 ToolRuntime 大小限制与 artifact 策略。

委派是有副作用的工具，默认不自动重试，避免重复生成 Agent 和重复计费。调度器使用异步任务和信号量限制并发；子 Agent 不获得委派工具，无法递归生成新 Agent。

## 权限与预算

首版 Sub Agent 只能调用 `read_file`、`search` 和只读 `git`，继承主运行的工具 denylist，不继承写入、Shell、自定义额外能力或工具网络授权。多个子 Agent 可以同时分析同一个工作区；文件修改由主 Agent 完成。Harness 的 Trace、Session 和 artifact 持久化仍由受控 adapter 完成。模型 provider 的授权连接沿用模型 adapter 配置。本地命令 adapter 的隔离限制仍见 [安全边界](sandbox-security.md)。

团队共用主运行的四层预算：主模型和子模型每次调用共用总步数，所有已报告的 Token 和成本累计，主工具和子工具共用调用额度，模型等待与委派都受团队墙钟截止时间限制。每个子 Agent 另有表中的四层预算。没有隐式增加的子预算额度。

模型响应的 Token 数在响应后才能确定，并行的已启动响应可能使总 Token 跨过上限；达到上限后禁止继续启动调用。真实 provider 取消后的实际计费若未返回 usage，Harness 无法精确补计，Trace 会保留取消事实。

单个子 Agent 达到预算或超时时返回 `budget_exhausted`，主 Agent 可以继续；团队总预算耗尽则主运行以 `budget_exhausted` 结束。取消主运行会取消并等待所有已启动子任务完成清理，取消预算不会留下后台 Agent。每个主运行重新建立独立预算与累计委派计数；同一 Runtime 的主运行串行执行。

## Trace 与结果

每个 Agent 有独立 `run_id` 和自己的单调递增 `step_id`。子事件包含 `parent_run_id`，父运行记录 `sub_agent_started` / `sub_agent_finished`，关联子 ID、角色、任务名与委派调用。终态、结果和 Session 相互一致。

主运行的 `RunResult.token_usage`、`cost_usd` 和 `tool_calls` 为团队总量；`steps` 仍为主 Agent 的步数，便于与它的事件 `step_id` 对照。总模型调用步数、子 Agent 数量在主 `run_finished` 事件的 `team_steps` / `sub_agents` 中记录。主运行的延迟分解保留主视角，委派等待计入工具时间；子模型的延迟可从各自 Trace 查看，不能把并行子延迟简单相加当作团队墙钟时间。

Trace JSONL 同时保留父子事实。默认 replay/diff 选择主运行，主运行的 Token/成本摘要使用团队总量，逐步视图展示主 Agent 的步骤。可指定一个子运行回放：

```bash
uv run mini-dsh trace replay .minidsh/traces/PARENT.jsonl --run-id CHILD --json
```

Session 格式新增缓存 Token 与完整延迟字段，仍可读取旧版本记录。

## Python API 与离线示例

```python
from minidsh.collaboration import build_multi_agent_runtime
from minidsh.composition import RuntimeOptions

runtime = build_multi_agent_runtime(
    workspace=workspace,
    model=main_model,
    subagent_model_factory=create_independent_model,
    options=RuntimeOptions(subagent_max_agents=4, subagent_max_parallel=2),
    budget=team_budget,
    subagent_pricing=child_rate_card,
)
result = await runtime.agent.run("Analyze and review the implementation")
```

工厂每次须返回新的模型 adapter；它可以采用与主模型不同的 provider。显式调用这个组装函数即启用协作；`build_local_runtime` 接收到 `multi_agent_enabled=true` 会报错，避免静默忽略设置。事件总线可通过 `events=` 替换。

无需 API Key 的完整示例会在临时工作区启动两个 Sub Agent，各自通过工具读取文件，并验证二者完成、团队工具调用数和主运行终态：

```bash
uv run python examples/multi_agent.py
```

离线 CLI 使用 `--script examples/multi_agent_responses.json --subagent-script examples/subagent_responses.json --multi-agent`；每个子 Agent 单独加载同一个子脚本。示例脚本要求工作区有 `hello.py`，上述 Python 示例会自动准备它。

当前协作入口支持 `run/chat/gui`。已有 benchmark/ablation 保持单 Agent 基线，并显式拒绝协作选项；公平的 Multi-Agent 实验仍需单独固定团队预算、任务划分和成功判据。本版不提供并行写入、独立 worktree 合并、Agent 间任意消息或分布式调度，后续扩展须先定义相应的权限与一致性契约。

## GUI 设置

运行 `uv run mini-dsh gui`，打开侧边栏“运行设置”，输入 API Key、启用 Multi-Agent，
选择子 Agent 数量与并发上限后保存。Key 也可由启动环境提供。当前任务结束后才能更改设置，
保存会保留对话；Sub Agent 卡片展示每个子任务的状态与结果。子 Agent 仍仅使用只读工具，
写入与命令权限由启动参数授予。离线脚本模式需同时提供 `--subagent-script`，不需要 API Key。

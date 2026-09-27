# 自定义运行时与配置方案

Mini-dsh 的核心 Loop 保持单 Agent、单工作区；可选的 [Multi-Agent 编排层](multi-agent.md) 在端口之上协作多个独立 Loop。CLI 用于选择内置配置；Python 组装 API 用于接入自己的组件和插件。插件是调用方信任的 Python 代码，并不在隔离环境中运行；工具本身负责通过受控 adapter 执行外部操作。

## 命名配置方案

完整例子见 [`examples/profiles.toml`](../examples/profiles.toml)。把它复制到工作区的 `minidsh.toml`，或显式指定文件：

```bash
uv run mini-dsh config --config examples/profiles.toml --profile review
uv run mini-dsh chat --config examples/profiles.toml --profile edit
uv run mini-dsh config --config examples/profiles.toml --profile edit --no-allow-write
```

```toml
[minidsh]
profile = "review"       # 可选的默认方案
allow_write = false
allow_shell = false
max_steps = 30

[profiles.review]
context_strategy = "summary"
recent_messages = 8
enabled_tools = ["read_file", "search", "git"]

[profiles.edit]
allow_write = true
context_strategy = "retrieval_summary"
enabled_tools = ["read_file", "search", "git", "patch_file", "write_file"]
```

选择顺序为 `--profile > MINIDSH_PROFILE > [minidsh].profile`。字段合并顺序为 `CLI > 环境变量 > 已选方案 > 基础配置 > 安全默认值`。方案是对基础配置的浅层覆盖，列表整体替换；不支持隐式继承。所有方案都校验字段和数值，未选方案中的拼写错误也会报错。`config` 输出当前方案、可选方案、生效值和每个覆盖字段的来源。

方案只包含运行时参数，模型仍使用已有的 `--provider`、`--model-profile`、`--endpoint` 等模型 adapter 选项；两种 profile 不混用。

## 可调参数

| 能力 | 配置字段 / CLI |
| --- | --- |
| Context | `context_strategy` / `--context`，`sliding_window`，`recent_messages` |
| Agent 行为 | `system_prompt`，`reflection_enabled`，`detailed_tool_feedback` |
| 工具选择 | `enabled_tools` / `--enabled-tools TOOL ...`，`builtin_tools_enabled` / `--[no-]builtin-tools` |
| 权限 | `allow_write`，`allow_shell`，`denied_tools` / `--deny-tool`，`extra_permissions` / `--extra-permission` |
| 命令与隔离 | `allowed_commands` / `--allow-command`，`test_command`，`build_command`，`sandbox_backend`，`docker_image`，`network_rules` / `--allow-host` |
| 输出与重试 | `tool_output_limit`，`artifact_offload`，`tool_max_retries` |
| 预算 | `max_steps`，`max_tokens`，`max_tool_calls`，`max_seconds` / `--max-seconds` |
| 成本 | `input_usd_per_million`，`output_usd_per_million`，`cached_input_usd_per_million` |

布尔 CLI 参数可以显式开关，例如 `--no-allow-shell`、`--no-reflection-enabled`。工具选择不会自动授予权限：选择 `write_file` 仍需 `allow_write = true`，选择 `shell/test/build` 仍需 `allow_shell = true` 和对应命令白名单。未知工具或未授权的内置工具会在组装阶段报错。`enabled_tools` 未设置时保留默认工具集，空列表则关闭所有工具；在 `run` 命令中把可变长的 `--enabled-tools` 放在任务参数之后。

`extra_permissions` 专供自定义工具声明的能力，默认没有任何额外授权。内置能力 `fs.read/fs.write/git.read/process.exec` 不允许通过它设置，写入和进程权限使用专用选项。`denied_tools` 优先于所有能力授权。

环境变量统一加 `MINIDSH_` 并将配置字段大写；列表使用 JSON 数组，避免逗号、空格和 Shell 语法的歧义。`max_duration_seconds` 使用 `MINIDSH_MAX_SECONDS`。

```bash
MINIDSH_PROFILE=review uv run mini-dsh config --config examples/profiles.toml
MINIDSH_ENABLED_TOOLS='["read_file","search"]' uv run mini-dsh config
MINIDSH_ALLOWED_COMMANDS='["git","python"]' uv run mini-dsh config
MINIDSH_TEST_COMMAND='["python","-m","pytest","-q"]' uv run mini-dsh config
```

## 显式替换组件

`build_local_runtime(..., components=RuntimeComponents(...), events=...)` 接受自己的 Context、Session、Permission、ProcessRunner 和工具集合；模型仍通过 `model=` 注入。Context 策略名只决定默认实现；显式注入的 Context 优先。

```python
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime

runtime = build_local_runtime(
    workspace=workspace,
    model=my_model,
    options=RuntimeOptions(builtin_tools_enabled=False),
    components=RuntimeComponents(
        context=my_context,
        sessions=my_sessions,
        permissions=my_permission_policy,
        runner=my_process_runner,
        tools=(my_tool,),
    ),
    events=my_event_stream,
)
result = await runtime.agent.run("Complete the task")
```

工具总是通过统一 ToolRuntime 执行 Schema 校验、权限判断、超时、Observation 和事件记录。自定义权限策略与本次运行的基础策略取交集，只能收紧授权。关闭内置工具后可以注册同名替代工具；未关闭时重复名称会被拒绝。自己的 EventStream 必须提供 `publish` 和 `subscribe`，并遵守订阅者错误隔离契约。直接注入的资源由调用方管理生命周期。

## 插件服务与生命周期

`plugin_runtime` 将已有的 PluginManager 接入实际组装流程：先校验完整依赖图，按依赖顺序启动，再按 `RuntimeServices` 中的显式服务名选择实现。服务不存在或不符合运行时协议会在运行前失败并清理资源。服务键不设置时使用本地默认实现；模型必须显式注入或由插件提供。

| RuntimeServices 字段 | 服务契约 |
| --- | --- |
| `model` | `ports.Model` |
| `context` | `ports.ContextManager` |
| `sessions` | `ports.SessionStore` |
| `permissions` | `tools.PermissionAuthorizer` |
| `runner` | `infrastructure.ProcessRunner` |
| `events` | `ports.EventStream` |
| `tools` | `composition.ToolBundle`，包含一组 `tools.Tool` |

插件须声明 `PluginManifest(plugin_id, version, provides, requires)` 并实现 `start(services)` 和 `stop()`。构造函数接收配置，不读取隐式全局配置，不通过导入副作用注册。`stop()` 必须支持启动中途失败后的清理。

```python
from minidsh.composition import RuntimeOptions, RuntimeServices, plugin_runtime

async with plugin_runtime(
    workspace=workspace,
    options=RuntimeOptions(builtin_tools_enabled=False),
    plugins=(my_plugin,),
    selection=RuntimeServices(model="my_model", context="my_context", tools="my_tools"),
) as runtime:
    result = await runtime.agent.run("Complete the task")
```

上下文退出时逆序停止插件，包括组装失败、调用方异常和取消。运行期间不暴露热替换入口，避免在工具调用或事务进行中换掉服务。插件目前通过 Python API 显式传入；CLI 不从仓库配置自动导入或执行插件代码。

无需模型服务的完整例子：

```bash
uv run python examples/custom_runtime.py
```

该例子在临时工作区中注册自己的模型、上下文和工具，关闭内置工具，为 `example.inspect` 显式授权，执行完整 Loop 后释放插件。

## 协作扩展

使用 `--multi-agent` 或 `[profiles.team]` 启用 Sub Agent 协作。模型工厂、并发、预算和 Trace 行为详见 [Multi-Agent 文档](multi-agent.md)。

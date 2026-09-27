# DSH 架构对齐与 Harness 研究

Mini-dsh 同时支持两种工作：用固定模型比较 Harness 策略，以及学习和实现 DSH 的
插件化 Runtime。当前为 Python 独立实现，不是官方 DSH 的 Python SDK，也不直接加载
Cordis/npm 插件。

## 共同执行路径

```text
提交 Session 消息事件
  → 从事件投影原始历史
  → Context 策略生成模型视图
  → Model
  → ToolRuntime（Schema → 权限 → 超时执行 → Observation）
  → 提交反馈事件 → 下一步或终态
```

Session 消息日志在 `.minidsh/session_events/`，完整执行 Trace 沿用原有位置。
终态 JSON 是兼容快照。新运行恢复历史时优先读取消息日志；旧快照继续可用。
日志重复创建、损坏、序号不连续或不完整尾部都会明确失败。恢复不重放工具；新轮
补充“执行结果未知”的消息，保留上一轮原始记录。当前每轮有独立 run 日志，导入
上一轮历史作为初始事实，没有实现官方跨轮 Session ID 和分叉协议。

## 替换 Loop

```python
from minidsh.application.agent_loop import AgentLoopFactory
from minidsh.composition import RuntimeServices, plugin_runtime
from minidsh.ports import AgentDependencies, AgentRunner
from minidsh.runtime import PluginManifest, ServiceRegistry


class CustomLoopPlugin:
    manifest = PluginManifest("custom_loop", "1.0.0", provides=("custom_loop_factory",))

    async def start(self, services: ServiceRegistry) -> None:
        services.register("custom_loop_factory", self)

    async def stop(self) -> None:
        pass

    def create(self, dependencies: AgentDependencies) -> AgentRunner:
        # 在这里替换驱动。保留预算、终态、事件和工具链路契约。
        return AgentLoopFactory().create(dependencies)


# 在 async 函数中，以已有 workspace、model 和 RuntimeOptions 组装：
# async with plugin_runtime(
#     workspace=workspace, model=model, options=options,
#     plugins=(CustomLoopPlugin(),),
#     selection=RuntimeServices(agent_factory="custom_loop_factory"),
# ) as runtime:
#     result = await runtime.agent.run("task")
```

`RuntimeServices.journal` 可选择插件提供的 Journal；`RuntimeComponents.journal`
和 `agent_factory` 则用于直接注入。默认的 `plugin_runtime` 自动注册
`AgentLoopPlugin`，按依赖启动并逆序释放。外部工厂是可信代码，必须遵守已有端口契约。

## 接入 MCP 工具

安装可选依赖：

```bash
uv sync --extra mcp
```

以下代码通过明确的本地 HTTP 端点授权接入一个已启动的 MCP 服务：

```python
from pathlib import Path

from minidsh.adapters.mcp import McpPlugin
from minidsh.adapters.mcp_http import HttpMcpClient
from minidsh.composition import RuntimeOptions, RuntimeServices, plugin_runtime
from minidsh.infrastructure import NetworkPolicy, NetworkRule
from minidsh.ports import Model


async def run_with_mcp(workspace: Path, model: Model) -> str:
    network = NetworkPolicy(
        frozenset(
            {
                NetworkRule("127.0.0.1", ports=frozenset({8000})),
            }
        )
    )
    client = HttpMcpClient("http://127.0.0.1:8000/mcp", network=network)
    async with plugin_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(extra_permissions=frozenset({"mcp.docs"})),
        plugins=(McpPlugin("docs", client),),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        return (await runtime.agent.run("使用文档工具回答当前问题")).answer
```

连接授权和工具执行授权分别显式声明。发现的 `lookup` 工具对模型名为
`mcp_docs_lookup`，需要 `mcp.docs` 权限。工具类型由 JSON Schema 验证；远端错误进入
结构化 Observation；结果沿用输出上限和 artifact 机制。所有远端工具默认有副作用，
不会自动重试，也不会因为服务声明只读就得到本地写入或 Shell 权限。

当前接入入口是 Python API，CLI/GUI 尚无 MCP 服务配置入口。实际 transport 支持
Streamable HTTP；不支持启动 stdio 服务。远端服务执行环境由服务部署方隔离，本地
WorkspaceSandbox 不能约束远端工具内部操作。HTTP 客户端拒绝未授权目标、凭据 URL、
代理环境继承和重定向；可通过内存 headers 显式提供认证，勿写入 Prompt 或 Trace。

无需真实模型或外部 MCP 服务的真实协议测试：

```bash
uv run pytest tests/test_mcp_http.py -q
```

该测试在临时工作区启动本地 MCP 服务，用脚本模型完成工具发现、授权、调用、反馈、
下一步、终态和资源释放。完整质量门禁仍使用仓库约定的四条命令；dev 依赖包含 MCP
协议测试所需包，基础运行依赖仍只有 Pydantic。

## 公平实验

- 保留现有 Context、工具输出、权限消融与成功判据，不把模型自报完成当成通过。
- 固定模型、采样参数、任务、预算、Journal 实现、Loop 工厂与执行隔离。
- 使用 MCP 时额外固定服务/协议版本、工具目录与输入数据；外部动态服务不用于确定性测试。
- 对比原实现与事件日志实现时，将日志方式作为唯一变量，并报告 Harness 耗时。
- 历史结果保留为旧基线；新实现需重新实测后才能引用其成功率或成本。

取舍及后续迁移见 [ADR 0012](adr/0012-session-facts-loop-factory-mcp.md)。

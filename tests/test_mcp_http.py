"""Real protocol integration using a temporary local MCP server only."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="install the mcp extra for protocol integration")

import uvicorn
from mcp.server.fastmcp import FastMCP

from minidsh.adapters import ScriptedModel
from minidsh.adapters.mcp import McpPlugin
from minidsh.adapters.mcp_http import HttpMcpClient
from minidsh.composition import RuntimeOptions, RuntimeServices, plugin_runtime
from minidsh.domain import ModelResponse, RunStatus, ToolCall
from minidsh.infrastructure import NetworkPolicy, NetworkRule


def test_http_mcp_refuses_implicit_network_access() -> None:
    with pytest.raises(ValueError, match="not explicitly authorized"):
        HttpMcpClient("https://example.com/mcp", network=NetworkPolicy())


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_local_mcp_server_drives_the_full_budgeted_loop(tmp_path: Path) -> None:
    mcp = FastMCP("offline_test", stateless_http=True, json_response=True)

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                    pytest.fail("server exited before startup")
                await asyncio.sleep(0.01)
        client = HttpMcpClient(
            f"http://127.0.0.1:{port}/mcp",
            network=NetworkPolicy(frozenset({NetworkRule("127.0.0.1", ports=frozenset({port}))})),
        )
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel(
                [
                    ModelResponse(tool_calls=(ToolCall("a", "mcp_local_add", {"a": 2, "b": 3}),)),
                    ModelResponse("sum is 5"),
                ]
            ),
            options=RuntimeOptions(extra_permissions=frozenset({"mcp.local"})),
            plugins=(McpPlugin("local", client),),
            selection=RuntimeServices(tools="mcp_local_tools"),
        ) as runtime:
            result = await runtime.agent.run("add two and three")
            assert result.status is RunStatus.FINISHED
            assert result.tool_calls == 1
            assert '"result": 5' in result.history[-2].content
            assert (await runtime.journal.open(result.run_id)).messages() == result.history
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await server_task
        finally:
            if not server_task.done():
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
            listener.close()

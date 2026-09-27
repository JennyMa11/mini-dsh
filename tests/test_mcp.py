"""MCP contracts with a deterministic transport, without external services."""

import asyncio
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.mcp import McpPlugin, McpToolSpec
from minidsh.composition import RuntimeOptions, RuntimeServices, plugin_runtime
from minidsh.domain import JsonValue, ToolCall, ToolStatus
from minidsh.tools import ToolError


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        return (
            McpToolSpec(
                "lookup",
                "Lookup documentation",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        )

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "documentation"}]}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_mcp_tools_validate_and_require_explicit_server_grant(tmp_path: Path) -> None:
    client = FakeMcpClient()
    plugin = McpPlugin("docs", client)
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(),
        plugins=(plugin,),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        invalid = await runtime.tools.execute(ToolCall("a", "mcp_docs_lookup", {}), "r", 1)
        denied = await runtime.tools.execute(
            ToolCall("b", "mcp_docs_lookup", {"query": "python"}),
            "r",
            1,
        )
        assert invalid.status is ToolStatus.INVALID
        assert denied.status is ToolStatus.DENIED
        assert client.calls == []
    assert client.closed


@pytest.mark.asyncio
async def test_mcp_result_uses_bounded_standard_observation(tmp_path: Path) -> None:
    client = FakeMcpClient()
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(extra_permissions=frozenset({"mcp.docs"}), tool_output_limit=10),
        plugins=(McpPlugin("docs", client),),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        result = await runtime.tools.execute(
            ToolCall("a", "mcp_docs_lookup", {"query": "python"}),
            "r",
            1,
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert result.truncated
        assert result.artifacts
        assert client.calls == [("lookup", {"query": "python"})]
        assert next(
            t for t in runtime.tools.definitions() if t.name == "mcp_docs_lookup"
        ).side_effects


@pytest.mark.asyncio
async def test_failed_mcp_discovery_closes_connection(tmp_path: Path) -> None:
    class BrokenClient(FakeMcpClient):
        async def list_tools(self) -> tuple[McpToolSpec, ...]:
            raise RuntimeError("discovery failed")

    client = BrokenClient()
    with pytest.raises(RuntimeError, match="discovery failed"):
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(),
            plugins=(McpPlugin("docs", client),),
        ):
            pytest.fail("startup must fail")
    assert client.closed


@pytest.mark.parametrize("arguments", [{"query": 7}, {"query": "x", "extra": True}])
@pytest.mark.asyncio
async def test_mcp_schema_rejects_wrong_types_and_extra_fields(
    tmp_path: Path,
    arguments: dict[str, JsonValue],
) -> None:
    client = FakeMcpClient()
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(extra_permissions=frozenset({"mcp.docs"})),
        plugins=(McpPlugin("docs", client),),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        result = await runtime.tools.execute(ToolCall("c", "mcp_docs_lookup", arguments), "r", 1)
        assert result.status is ToolStatus.INVALID
        assert client.calls == []


@pytest.mark.asyncio
async def test_remote_errors_are_observations_and_side_effects_are_not_retried(
    tmp_path: Path,
) -> None:
    class FailingClient(FakeMcpClient):
        async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
            self.calls.append((name, arguments))
            raise ToolError("mcp_tool_error", "remote failure", retryable=True)

    client = FailingClient()
    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(extra_permissions=frozenset({"mcp.docs"}), tool_max_retries=5),
        plugins=(McpPlugin("docs", client),),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        result = await runtime.tools.execute(
            ToolCall("c", "mcp_docs_lookup", {"query": "python"}),
            "r",
            1,
        )
        assert result.status is ToolStatus.FAILED
        assert result.error is not None
        assert result.error.code == "mcp_tool_error"
        assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_mcp_startup_timeout_rolls_back_the_connection(tmp_path: Path) -> None:
    class HangingClient(FakeMcpClient):
        async def list_tools(self) -> tuple[McpToolSpec, ...]:
            await asyncio.Event().wait()
            return ()

    client = HangingClient()
    with pytest.raises(TimeoutError):
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(),
            plugins=(McpPlugin("docs", client, startup_timeout_seconds=0.01),),
        ):
            pytest.fail("startup must time out")
    assert client.closed


@pytest.mark.asyncio
async def test_remote_schema_cannot_trigger_external_reference_access(tmp_path: Path) -> None:
    class ReferencingClient(FakeMcpClient):
        async def list_tools(self) -> tuple[McpToolSpec, ...]:
            return (
                McpToolSpec(
                    "lookup",
                    "docs",
                    {
                        "type": "object",
                        "properties": {"query": {"$ref": "file:///private/schema"}},
                    },
                ),
            )

    client = ReferencingClient()
    with pytest.raises(ValueError, match="external references"):
        async with plugin_runtime(
            workspace=tmp_path,
            model=ScriptedModel([]),
            options=RuntimeOptions(),
            plugins=(McpPlugin("docs", client),),
        ):
            pytest.fail("external references must be rejected during discovery")
    assert client.closed


@pytest.mark.asyncio
async def test_remote_credentials_are_removed_before_feedback_and_artifact_storage(
    tmp_path: Path,
) -> None:
    class CredentialClient(FakeMcpClient):
        async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
            del name, arguments
            return {"api_key": "private-value", "text": "Bearer private-value"}

    async with plugin_runtime(
        workspace=tmp_path,
        model=ScriptedModel([]),
        options=RuntimeOptions(extra_permissions=frozenset({"mcp.docs"}), tool_output_limit=10),
        plugins=(McpPlugin("docs", CredentialClient()),),
        selection=RuntimeServices(tools="mcp_docs_tools"),
    ) as runtime:
        result = await runtime.tools.execute(
            ToolCall("c", "mcp_docs_lookup", {"query": "python"}),
            "r",
            1,
        )
        assert result.status is ToolStatus.SUCCEEDED
        for artifact in result.artifacts:
            assert "private-value" not in (tmp_path / artifact).read_text()

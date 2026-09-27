"""Optional official MCP SDK transport for explicitly authorized HTTP servers."""

from __future__ import annotations

import asyncio
import math
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import cast
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from minidsh.adapters.mcp import McpToolSpec
from minidsh.domain import JsonValue
from minidsh.infrastructure import NetworkPolicy
from minidsh.tools import ToolError

MAX_MCP_TOOLS = 256
MAX_MCP_PAGES = 32


class HttpMcpClient:
    """No redirects, proxy inheritance, implicit credentials, or process launch."""

    def __init__(
        self,
        url: str,
        *,
        network: NetworkPolicy,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP URL must be HTTP or HTTPS")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP URL cannot contain credentials or a fragment")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not network.authorizes(parsed.hostname, protocol="tcp", port=port):
            raise ValueError("MCP endpoint is not explicitly authorized by network policy")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("MCP timeout must be positive")
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        """Initialize the SDK transport under a bounded startup timeout."""
        if self._stack is not None:
            raise ValueError("MCP client already connected")
        self._stack = AsyncExitStack()
        # SDK/anyio contexts are entered and exited by the same plugin lifecycle task.
        http_client = await self._stack.enter_async_context(
            httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
                trust_env=False,
            )
        )
        streams = await self._stack.enter_async_context(
            streamable_http_client(self._url, http_client=http_client)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self._timeout)
            )
        )
        await self._session.initialize()

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        """Discover a bounded catalog including pagination."""
        session = self._require_session()
        specs: list[McpToolSpec] = []
        cursor: str | None = None
        cursors: set[str] = set()
        async with asyncio.timeout(self._timeout):
            while True:
                page = await session.list_tools(cursor=cursor)
                specs.extend(
                    McpToolSpec(
                        tool.name,
                        tool.description or "",
                        cast("dict[str, JsonValue]", tool.inputSchema),
                    )
                    for tool in page.tools
                )
                if len(specs) > MAX_MCP_TOOLS:
                    raise ValueError("MCP tool catalog exceeds 256 tools")
                cursor = page.nextCursor
                if cursor is None:
                    return tuple(specs)
                if cursor in cursors or len(cursors) >= MAX_MCP_PAGES:
                    raise ValueError("invalid or excessive MCP pagination")
                cursors.add(cursor)

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        """Keep remote errors safe and disable automatic side-effect retries."""
        try:
            result = await self._require_session().call_tool(name, arguments)
        except Exception as exc:
            raise ToolError("mcp_transport_error", "MCP request failed") from exc
        if result.isError:
            raise ToolError("mcp_tool_error", "MCP server reported a tool error")
        return cast("JsonValue", result.model_dump(mode="json", by_alias=True))

    async def close(self) -> None:
        """Release the session and underlying HTTP task group."""
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await stack.aclose()

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP client is not connected")
        return self._session

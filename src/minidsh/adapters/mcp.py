"""MCP tool projection with explicit capabilities and managed lifecycle."""

from __future__ import annotations

import asyncio
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol, Self, cast

from pydantic import BaseModel, RootModel, model_validator

from minidsh.domain import JsonValue, ToolDefinition, redact_value
from minidsh.runtime import PluginManifest, ServiceRegistry
from minidsh.tools import Tool, ToolBundle

MAX_MCP_TOOLS = 256
MAX_MODEL_TOOL_NAME = 64


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """Discovered metadata; remote annotations never grant permissions."""

    name: str
    description: str
    input_schema: dict[str, JsonValue]


class McpClient(Protocol):
    """An explicitly authorized connection implemented by transport adapters."""

    async def connect(self) -> None:
        """Connect and initialize a trusted, explicitly configured server."""

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        """Discover bounded tool metadata."""

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        """Call a tool; protocol errors must become structured ToolError values."""

    async def close(self) -> None:
        """Release the connection and all transport tasks."""


def _input_model(schema: dict[str, JsonValue]) -> type[BaseModel]:
    # JSON Schema is an optional transport-boundary dependency, never a domain import.
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - optional dependency

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(deepcopy(schema))

    # Reject reference-based filesystem/network resolution. Local references still work.
    def check_references(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("MCP schema external references are unsupported")
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(schema)

    class McpArguments(RootModel[dict[str, JsonValue]]):
        @model_validator(mode="after")
        def validate_schema(self) -> Self:
            try:
                invalid = next(validator.iter_errors(self.root), None) is not None
            except Exception as exc:
                raise ValueError("MCP schema validation failed") from exc
            if invalid:
                raise ValueError("arguments do not satisfy the MCP tool schema")
            return self

    return McpArguments


class McpTool:
    """Remote execution behind the ordinary schema/permission/timeout pipeline."""

    def __init__(self, server_id: str, spec: McpToolSpec, client: McpClient) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", server_id):
            raise ValueError("MCP server_id must be snake_case")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", spec.name):
            raise ValueError("invalid MCP tool name")
        self._client = client
        self._remote_name = spec.name
        self.input_model = _input_model(spec.input_schema)
        model_name = f"mcp_{server_id}_{spec.name.lower().replace('-', '_').replace('.', '_')}"
        if len(model_name) > MAX_MODEL_TOOL_NAME:
            raise ValueError("MCP model tool name exceeds 64 characters")
        self.definition = ToolDefinition(
            model_name,
            spec.description,
            deepcopy(spec.input_schema),
            # Unknown remote behavior is conservatively side-effecting: no auto-retry.
            side_effects=True,
            permissions=(f"mcp.{server_id}",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        """Send only arguments already validated and authorized by ToolRuntime."""
        result = await self._client.call_tool(
            self._remote_name, cast("dict[str, JsonValue]", arguments.model_dump())
        )
        return cast("JsonValue", redact_value(result))


class McpPlugin:
    """Discover tools at startup, then inject their bundle into the standard registry."""

    def __init__(
        self,
        server_id: str,
        client: McpClient,
        *,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", server_id):
            raise ValueError("MCP server_id must be snake_case")
        if not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0:
            raise ValueError("MCP startup timeout must be finite and positive")
        self._startup_timeout = startup_timeout_seconds
        self._server_id = server_id
        self._client = client
        self.manifest = PluginManifest(
            f"mcp_{server_id}", "1.0.0", provides=(f"mcp_{server_id}_tools",)
        )

    async def start(self, services: ServiceRegistry) -> None:
        """Connect and validate discovered definitions before registering the bundle."""
        async with asyncio.timeout(self._startup_timeout):
            await self._client.connect()
            specs = await self._client.list_tools()
            if len(specs) > MAX_MCP_TOOLS:
                raise ValueError("MCP tool catalog exceeds 256 tools")
            tools: tuple[Tool, ...] = tuple(
                McpTool(self._server_id, spec, self._client) for spec in specs
            )
        names = [tool.definition.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate normalized MCP tool names")
        services.register(self.manifest.provides[0], ToolBundle(tools))

    async def stop(self) -> None:
        """Close even a partially initialized transport during startup rollback."""
        await self._client.close()

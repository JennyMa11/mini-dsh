"""Run an explicit plugin with a custom model, context, and permissioned toolset.

Run from the repository root: uv run python examples/custom_runtime.py
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, RuntimeServices, ToolBundle, plugin_runtime
from minidsh.context import SlidingWindowContext
from minidsh.domain import ModelResponse, RunStatus, ToolCall
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.runtime import PluginManifest, ServiceRegistry
from minidsh.tools import ReadFileTool


class ExamplePlugin:
    """Explicitly constructed adapters, with no global or import-time registration."""

    manifest = PluginManifest(
        "example_plugin",
        "1.0.0",
        provides=("example_model", "example_context", "example_tools"),
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def start(self, services: ServiceRegistry) -> None:
        tool = ReadFileTool(WorkspaceSandbox(self._workspace))
        tool.definition = replace(tool.definition, permissions=("example.inspect",))
        services.register("example_tools", ToolBundle((tool,)))
        services.register("example_context", SlidingWindowContext(8))
        services.register(
            "example_model",
            ScriptedModel(
                [
                    ModelResponse(
                        tool_calls=(ToolCall("inspect", "read_file", {"path": "hello.txt"}),)
                    ),
                    ModelResponse("Inspected hello.txt through the plugin tool."),
                ]
            ),
        )

    async def stop(self) -> None:
        """No resources to release; network clients would be closed here."""


async def main() -> None:
    """Compose and run an isolated, offline example, then release the plugin."""
    with TemporaryDirectory(prefix="minidsh-plugin-") as directory:
        workspace = Path(directory)
        (workspace / "hello.txt").write_text("Hello from a plugin!\n", encoding="utf-8")
        async with plugin_runtime(
            workspace=workspace,
            options=RuntimeOptions(
                builtin_tools_enabled=False,
                extra_permissions=frozenset({"example.inspect"}),
                system_prompt="Inspect the workspace using the provided tools.",
            ),
            plugins=(ExamplePlugin(workspace),),
            selection=RuntimeServices(
                model="example_model", context="example_context", tools="example_tools"
            ),
        ) as runtime:
            result = await runtime.agent.run("Read hello.txt.")
            if result.status is not RunStatus.FINISHED or result.tool_calls != 1:
                raise RuntimeError(f"example failed: {result.failure_reason}")
            print(f"{result.status.value}: {result.answer}")


if __name__ == "__main__":
    asyncio.run(main())

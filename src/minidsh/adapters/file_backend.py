"""Local and container file adapters behind one asynchronous operation contract."""

import asyncio
import json
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from minidsh.domain import JsonValue, ToolDefinition
from minidsh.infrastructure import ProcessRunner
from minidsh.infrastructure.file_worker import dispatch
from minidsh.ports.files import FileBackend
from minidsh.tools.runtime import PreparedTool, Tool, ToolError

_SKILL_ERRORS = frozenset(
    {
        "skill_path_invalid",
        "skill_source_denied",
        "skill_size_limit",
        "skill_changed",
        "skill_encoding_invalid",
        "skill_root_missing",
        "skill_source_cycle",
        "skill_duplicate_source",
        "skill_count_limit",
        "skill_roots_invalid",
        "skill_read_invalid",
    }
)
_SKILL_OPERATIONS = frozenset({"skill_scan", "skill_read"})


class LocalFileBackend:
    def __init__(self, tools: tuple[Tool, ...], *, workspace: Path | None = None) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}
        self._workspace = workspace.resolve(strict=True) if workspace is not None else None

    async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue:
        if operation in _SKILL_OPERATIONS:
            if self._workspace is None:
                raise ToolError("skill_source_unavailable", "skill_source_unavailable")
            try:
                return cast(
                    "JsonValue",
                    await asyncio.to_thread(dispatch, self._workspace, operation, arguments),
                )
            except (OSError, UnicodeError, ValueError) as exc:
                code = str(exc) if str(exc) in _SKILL_ERRORS else "skill_source_denied"
                raise ToolError(code, code) from None
        tool = self._tools[operation]
        return await tool.execute(tool.input_model.model_validate(arguments))

    async def prepare(self, operation: str, arguments: dict[str, JsonValue]) -> tuple[str, str]:
        tool = self._tools[operation]
        if not isinstance(tool, PreparedTool):
            return operation, operation
        return await tool.prepare(tool.input_model.model_validate(arguments))


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: JsonValue = None
    error: str | None


class DockerFileBackend:
    def __init__(
        self,
        runner: ProcessRunner,
        *,
        worker_runtime: Literal["python", "node"] = "python",
        worker_root: str = "/workspace",
    ) -> None:
        if worker_runtime not in {"python", "node"}:
            raise ValueError("unsupported_file_worker_runtime")
        root = PurePosixPath(worker_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("file_worker_root_must_be_absolute")
        if worker_runtime == "python" and worker_root != "/workspace":
            raise ValueError("python_file_worker_uses_workspace_root")
        self._runner = runner
        self._worker_runtime = worker_runtime
        self._worker_root = worker_root
        self._worker = (
            files("minidsh.infrastructure")
            .joinpath("file_worker.js" if worker_runtime == "node" else "file_worker.py")
            .read_text()
        )

    async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue:
        encoded = json.dumps({"operation": operation, "arguments": arguments}, ensure_ascii=False)
        command = (
            ("node", "-e", self._worker, encoded, self._worker_root)
            if self._worker_runtime == "node"
            else ("python3", "-c", self._worker, encoded)
        )
        result = await self._runner.run(command)
        if result.timed_out or result.exit_code != 0:
            raise ToolError("file_backend_failed", "container file operation failed")
        parsed = _Result.model_validate_json(result.stdout)
        if parsed.error is not None:
            if operation in _SKILL_OPERATIONS:
                code = parsed.error if parsed.error in _SKILL_ERRORS else "skill_source_denied"
                raise ToolError(code, code)
            code = (
                parsed.error
                if parsed.error
                in {"patch_mismatch", "patch_ambiguous", "file_exists", "invalid_range"}
                else "file_backend_denied"
            )
            raise ToolError(code, parsed.error)
        return parsed.result

    async def prepare(self, operation: str, arguments: dict[str, JsonValue]) -> tuple[str, str]:
        result = await self.invoke("prepare", {"tool": operation, "arguments": arguments})
        if not isinstance(result, dict):
            raise ToolError("file_backend_failed", "invalid preflight result")
        return str(result["scope"]), str(result["preview"])


class BackendFileTool:
    def __init__(self, tool: Tool, backend: FileBackend) -> None:
        self.definition: ToolDefinition = tool.definition
        self.input_model = tool.input_model
        self._backend = backend

    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        return await self._backend.prepare(
            self.definition.name, cast("dict[str, JsonValue]", arguments.model_dump())
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        return await self._backend.invoke(
            self.definition.name, cast("dict[str, JsonValue]", arguments.model_dump())
        )

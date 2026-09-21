"""Built-in repository, process, and Git tools."""

from __future__ import annotations

import fnmatch
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import JsonValue, ToolDefinition
from minidsh.infrastructure import (
    CommandDeniedError,
    ProcessRunner,
    SandboxViolationError,
    WorkspaceSandbox,
)
from minidsh.tools.runtime import ToolError, schema_for


class StrictInput(BaseModel):
    """Reject unknown model-provided arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadFileInput(StrictInput):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_chars: int = Field(default=20_000, ge=1, le=200_000)


class ReadFileTool:
    input_model = ReadFileInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "read_file",
            "Read a bounded UTF-8 file segment from the workspace.",
            schema_for(self.input_model),
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("ReadFileInput", arguments)
        try:
            lines = self._sandbox.read_text(args.path).splitlines()
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        except (OSError, UnicodeError) as exc:
            raise ToolError("read_failed", str(exc)) from exc
        end = args.end_line or len(lines)
        if end < args.start_line:
            raise ToolError("invalid_range", "end_line must be greater than or equal to start_line")
        content = "\n".join(lines[args.start_line - 1 : end])
        return {
            "path": args.path,
            "start_line": args.start_line,
            "end_line": min(end, len(lines)),
            "content": content[: args.max_chars],
            "truncated": len(content) > args.max_chars,
        }


class WriteFileInput(StrictInput):
    path: str = Field(min_length=1)
    content: str
    overwrite: bool = False


class WriteFileTool:
    input_model = WriteFileInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "write_file",
            "Create a UTF-8 file atomically; overwrite requires an explicit flag.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("fs.write",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("WriteFileInput", arguments)
        try:
            destination = self._sandbox.resolve(args.path)
            if destination.exists() and not args.overwrite:
                raise ToolError("file_exists", "file exists and overwrite is false")
            self._sandbox.atomic_write(args.path, args.content)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        except OSError as exc:
            raise ToolError("write_failed", str(exc)) from exc
        return {"path": args.path, "bytes_written": len(args.content.encode())}


class PatchFileInput(StrictInput):
    path: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    replacement: str
    occurrence: int | None = Field(default=None, ge=1)


class PatchFileTool:
    input_model = PatchFileInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "patch_file",
            "Atomically replace one exact text occurrence in a workspace file.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("fs.write",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("PatchFileInput", arguments)
        try:
            original = self._sandbox.read_text(args.path)
            count = original.count(args.expected)
            if count == 0:
                raise ToolError("patch_mismatch", "expected text was not found")
            if args.occurrence is None:
                if count != 1:
                    raise ToolError(
                        "patch_ambiguous",
                        f"expected text occurs {count} times; specify occurrence",
                    )
                updated = original.replace(args.expected, args.replacement, 1)
                replaced = 1
            else:
                if args.occurrence > count:
                    raise ToolError("patch_mismatch", "requested occurrence does not exist")
                position = -len(args.expected)
                for _ in range(args.occurrence):
                    position = original.find(args.expected, position + len(args.expected))
                updated = (
                    original[:position]
                    + args.replacement
                    + original[position + len(args.expected) :]
                )
                replaced = args.occurrence
            self._sandbox.atomic_write(args.path, updated)
        except ToolError:
            raise
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        except (OSError, UnicodeError) as exc:
            raise ToolError("patch_failed", str(exc)) from exc
        return {"path": args.path, "occurrence": replaced, "bytes_written": len(updated.encode())}


class SearchInput(StrictInput):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "*"
    max_results: int = Field(default=100, ge=1, le=500)


class SearchTool:
    input_model = SearchInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "search",
            "Search workspace text files for a literal string.",
            schema_for(self.input_model),
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("SearchInput", arguments)
        try:
            root = self._sandbox.resolve(args.path, must_exist=True)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        results: list[JsonValue] = []
        candidates = (root,) if root.is_file() else root.rglob("*")
        for path in candidates:
            if len(results) >= args.max_results:
                break
            if not path.is_file() or not fnmatch.fnmatch(path.name, args.glob):
                continue
            relative = path.relative_to(self._sandbox.root)
            if any(part in {".git", ".minidsh", ".venv"} for part in relative.parts):
                continue
            try:
                self._sandbox.ensure_not_sensitive(path)
                text = path.read_text(encoding="utf-8")
                for number, line in enumerate(text.splitlines(), 1):
                    if args.query in line:
                        results.append(
                            {"path": relative.as_posix(), "line": number, "text": line[:500]}
                        )
                        if len(results) >= args.max_results:
                            break
            except (OSError, UnicodeError, SandboxViolationError):
                continue
        return {"matches": results, "truncated": len(results) >= args.max_results}


class ShellInput(StrictInput):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class ShellTool:
    input_model = ShellInput

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner
        self.definition = ToolDefinition(
            "shell",
            "Run an allowlisted argv array in the workspace without a shell.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("process.exec",),
            timeout_seconds=305,
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("ShellInput", arguments)
        try:
            result = await self._runner.run(
                tuple(args.argv), cwd=args.cwd, timeout_seconds=args.timeout_seconds
            )
        except CommandDeniedError as exc:
            raise ToolError("command_denied", str(exc)) from exc
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        except OSError as exc:
            raise ToolError("command_failed", str(exc)) from exc
        return {
            "argv": list(result.argv),
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }


class GitInput(StrictInput):
    action: Literal["status", "diff"]
    paths: list[str] = Field(default_factory=list)


class GitTool:
    input_model = GitInput

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner
        self.definition = ToolDefinition(
            "git",
            "Inspect Git status or diff without changing repository state.",
            schema_for(self.input_model),
            permissions=("git.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("GitInput", arguments)
        argv = (
            ("git", "status", "--short")
            if args.action == "status"
            else ("git", "diff", "--no-ext-diff", "--", *args.paths)
        )
        try:
            result = await self._runner.run(argv)
        except CommandDeniedError as exc:
            raise ToolError("command_denied", str(exc)) from exc
        except OSError as exc:
            raise ToolError("git_failed", str(exc)) from exc
        return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


class EmptyInput(StrictInput):
    pass


class RunCommandTool:
    """Expose a fixed test or build command with no model-controlled arguments."""

    input_model = EmptyInput

    def __init__(
        self,
        name: Literal["test", "build"],
        runner: ProcessRunner,
        argv: tuple[str, ...],
    ) -> None:
        self._runner = runner
        self._argv = argv
        self.definition = ToolDefinition(
            name,
            f"Run the configured {name} command.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("process.exec",),
            timeout_seconds=305,
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        try:
            result = await self._runner.run(self._argv, timeout_seconds=300)
        except CommandDeniedError as exc:
            raise ToolError("command_denied", str(exc)) from exc
        except OSError as exc:
            raise ToolError("command_failed", str(exc)) from exc
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }

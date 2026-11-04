"""Built-in repository, process, and Git tools."""

from __future__ import annotations

import ast
import difflib
import fnmatch
import json
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

_MAX_INDEXED_FILE_BYTES = 256_000


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
            version="1.1.0",
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("ReadFileInput", arguments)
        try:
            lines = self._sandbox.read_text(args.path).splitlines()
        except SandboxViolationError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                # Check confinement even when strict resolution stopped at a missing path.
                try:
                    self._sandbox.resolve(args.path)
                except SandboxViolationError as denied:
                    raise ToolError("sandbox_violation", str(denied)) from denied
                raise ToolError("file_not_found", f"file not found: {args.path}") from exc
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

    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        args = cast("WriteFileInput", arguments)
        destination = self._sandbox.resolve(args.path)
        self._sandbox.ensure_model_writable(destination)
        if destination.exists() and not args.overwrite:
            raise ToolError("file_exists", "file exists and overwrite is false")
        old = self._sandbox.read_text(args.path) if destination.exists() else ""
        preview = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                args.content.splitlines(keepends=True),
                fromfile=args.path,
                tofile=args.path,
            )
        )
        return str(destination), preview or f"Write {args.path}"

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("WriteFileInput", arguments)
        try:
            destination = self._sandbox.resolve(args.path)
            self._sandbox.ensure_model_writable(destination)
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

    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        args = cast("PatchFileInput", arguments)
        path = self._sandbox.resolve(args.path, must_exist=True)
        self._sandbox.ensure_model_writable(path)
        original = self._sandbox.read_text(args.path)
        updated, _ = self._updated(original, args)
        preview = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=args.path,
                tofile=args.path,
            )
        )
        return str(path), preview

    @staticmethod
    def _updated(original: str, args: PatchFileInput) -> tuple[str, int]:
        count = original.count(args.expected)
        if count == 0:
            raise ToolError("patch_mismatch", "expected text was not found")
        if args.occurrence is None:
            if count != 1:
                raise ToolError(
                    "patch_ambiguous", f"expected text occurs {count} times; specify occurrence"
                )
            return original.replace(args.expected, args.replacement, 1), 1
        if args.occurrence > count:
            raise ToolError("patch_mismatch", "requested occurrence does not exist")
        position = -len(args.expected)
        for _ in range(args.occurrence):
            position = original.find(args.expected, position + len(args.expected))
        updated = original[:position] + args.replacement + original[position + len(args.expected) :]
        return updated, args.occurrence

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("PatchFileInput", arguments)
        try:
            self._sandbox.ensure_model_writable(self._sandbox.resolve(args.path))
            original = self._sandbox.read_text(args.path)
            updated, replaced = self._updated(original, args)
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


class ListFilesInput(StrictInput):
    path: str = "."
    glob: str = "*"
    max_results: int = Field(default=200, ge=1, le=1_000)


class ListFilesTool:
    """Give the agent a bounded repository map before it requests file contents."""

    input_model = ListFilesInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "list_files",
            "List workspace-relative file paths without contents.",
            schema_for(self.input_model),
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("ListFilesInput", arguments)
        try:
            root = self._sandbox.resolve(args.path, must_exist=True)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        candidates = (root,) if root.is_file() else root.rglob("*")
        files: list[str] = []
        truncated = False
        for path in candidates:
            if not path.is_file() or not fnmatch.fnmatch(path.name, args.glob):
                continue
            try:
                resolved = self._sandbox.resolve(path.relative_to(self._sandbox.root).as_posix())
                relative = resolved.relative_to(self._sandbox.root)
                if any(
                    part in {".git", ".minidsh", ".venv", "node_modules", "dist"}
                    for part in relative.parts
                ):
                    continue
                self._sandbox.ensure_not_sensitive(resolved)
            except (SandboxViolationError, ValueError):
                continue
            if len(files) >= args.max_results:
                truncated = True
                break
            files.append(relative.as_posix())
        return {"files": cast("list[JsonValue]", sorted(files)), "truncated": truncated}


class SearchCodeInput(StrictInput):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "*"
    max_files: int = Field(default=30, ge=1, le=200)


class SearchCodeTool:
    """Return matching file counts so the agent can inspect selected ranges."""

    input_model = SearchCodeInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "search_code",
            "Search code and return candidate paths with match counts only.",
            schema_for(self.input_model),
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("SearchCodeInput", arguments)
        try:
            root = self._sandbox.resolve(args.path, must_exist=True)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        candidates = (root,) if root.is_file() else root.rglob("*")
        matches: list[dict[str, JsonValue]] = []
        truncated = False
        for path in candidates:
            if not path.is_file() or not fnmatch.fnmatch(path.name, args.glob):
                continue
            relative = path.relative_to(self._sandbox.root)
            if any(
                part in {".git", ".minidsh", ".venv", "node_modules", "dist"}
                for part in relative.parts
            ):
                continue
            try:
                if path.stat().st_size > _MAX_INDEXED_FILE_BYTES:
                    continue
                content = self._sandbox.read_text(relative.as_posix())
            except (OSError, UnicodeError, SandboxViolationError):
                continue
            count = content.count(args.query)
            if count:
                if len(matches) >= args.max_files:
                    truncated = True
                    break
                matches.append({"path": relative.as_posix(), "matches": count})
        matches.sort(key=lambda item: (-int(cast("int", item["matches"])), str(item["path"])))
        return {"candidates": cast("list[JsonValue]", matches), "truncated": truncated}


class FindSymbolInput(StrictInput):
    name: str = Field(min_length=1)
    path: str = "."
    max_results: int = Field(default=30, ge=1, le=200)


class FindSymbolTool:
    """Locate Python definitions and return their ranges without source bodies."""

    input_model = FindSymbolInput

    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox
        self.definition = ToolDefinition(
            "find_symbol",
            "Find Python class/function definitions by name.",
            schema_for(self.input_model),
            permissions=("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("FindSymbolInput", arguments)
        needle = args.name.rsplit(".", 1)[-1].lower()
        try:
            root = self._sandbox.resolve(args.path, must_exist=True)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        candidates = (root,) if root.is_file() else root.rglob("*.py")
        symbols: list[JsonValue] = []
        truncated = False
        for path in candidates:
            if not path.is_file() or path.suffix != ".py":
                continue
            relative = path.relative_to(self._sandbox.root)
            if any(part in {".git", ".minidsh", ".venv"} for part in relative.parts):
                continue
            try:
                tree = ast.parse(self._sandbox.read_text(relative.as_posix()))
            except (OSError, UnicodeError, SyntaxError, SandboxViolationError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and needle in node.name.lower()
                ):
                    if len(symbols) >= args.max_results:
                        truncated = True
                        break
                    symbols.append(
                        {
                            "path": relative.as_posix(),
                            "name": node.name,
                            "start_line": node.lineno,
                            "end_line": node.end_lineno,
                        }
                    )
            if truncated:
                break
        return {"symbols": symbols, "truncated": truncated}


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

    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        args = cast("ShellInput", arguments)
        validator = getattr(self._runner, "validate", None)
        if callable(validator):
            validator(tuple(args.argv), cwd=args.cwd)
        encoded = json.dumps({"argv": args.argv, "cwd": args.cwd}, sort_keys=True)
        return encoded, encoded

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


class OpenFileInput(StrictInput):
    path: str = Field(min_length=1)


class OpenFileTool:
    input_model = OpenFileInput

    def __init__(self, sandbox: WorkspaceSandbox, runner: ProcessRunner, opener: str) -> None:
        self._sandbox = sandbox
        self._runner = runner
        self._opener = opener
        self.definition = ToolDefinition(
            "open_file",
            "Open a workspace file or directory in the system's default application.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("process.exec",),
            timeout_seconds=15,
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        args = cast("OpenFileInput", arguments)
        try:
            resolved = self._sandbox.resolve(args.path, must_exist=True)
        except SandboxViolationError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                try:
                    self._sandbox.resolve(args.path)
                except SandboxViolationError as denied:
                    raise ToolError("sandbox_violation", str(denied)) from denied
                raise ToolError("file_not_found", f"file not found: {args.path}") from exc
            raise ToolError("sandbox_violation", str(exc)) from exc
        try:
            self._sandbox.ensure_not_sensitive(resolved)
        except SandboxViolationError as exc:
            raise ToolError("sandbox_violation", str(exc)) from exc
        if not resolved.is_file() and not resolved.is_dir():
            raise ToolError("not_openable", f"not a file or directory: {args.path}")
        try:
            result = await self._runner.run((self._opener, str(resolved)), timeout_seconds=10)
        except CommandDeniedError as exc:
            raise ToolError("command_denied", str(exc)) from exc
        except OSError as exc:
            raise ToolError("open_failed", str(exc)) from exc
        if result.timed_out:
            raise ToolError("open_timeout", f"opening {args.path} timed out")
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "opener exited non-zero"
            raise ToolError("open_failed", detail[:1_000])
        return {"path": args.path, "opened": True}


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
        *,
        cwd: str = ".",
        timeout_seconds: float = 300,
    ) -> None:
        self._runner = runner
        self._argv = argv
        self._cwd = cwd
        self._timeout = timeout_seconds
        self.definition = ToolDefinition(
            name,
            f"Run the configured {name} command.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("process.exec",),
            timeout_seconds=timeout_seconds + 5,
        )

    async def prepare(self, arguments: BaseModel) -> tuple[str, str]:
        del arguments
        validator = getattr(self._runner, "validate", None)
        if callable(validator):
            validator(self._argv, cwd=self._cwd)
        encoded = json.dumps({"argv": self._argv, "cwd": self._cwd}, sort_keys=True)
        return encoded, encoded

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        try:
            result = await self._runner.run(
                self._argv, cwd=self._cwd, timeout_seconds=self._timeout
            )
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

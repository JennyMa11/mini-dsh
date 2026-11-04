"""Security and contract tests for the tool execution boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from minidsh.domain import Event, ToolCall, ToolStatus
from minidsh.infrastructure import (
    CommandDeniedError,
    CommandPolicy,
    DockerCommandRunner,
    LocalCommandRunner,
    ProcessResult,
    SandboxViolationError,
    WorkspaceSandbox,
)
from minidsh.runtime import EventBus
from minidsh.tools import (
    OpenFileTool,
    PatchFileTool,
    PermissionPolicy,
    ReadFileTool,
    ShellTool,
    ToolError,
    ToolRegistry,
    ToolRuntime,
    WriteFileTool,
)
from minidsh.tools.builtin import OpenFileInput


def test_sandbox_rejects_parent_traversal_and_absolute_paths(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        sandbox.resolve("../secret")
    with pytest.raises(SandboxViolationError, match="absolute paths"):
        sandbox.resolve("/etc/passwd")


def test_command_policy_is_deny_first() -> None:
    policy = CommandPolicy(allowed_commands=frozenset({"git", "rm"}))

    policy.validate(("git", "status"))
    with pytest.raises(CommandDeniedError, match="denied"):
        policy.validate(("rm", "-rf", "anything"))
    with pytest.raises(CommandDeniedError, match="allowlisted"):
        policy.validate(("python", "-V"))


@pytest.mark.asyncio
async def test_tool_runtime_validates_authorizes_and_executes(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool(WorkspaceSandbox(tmp_path)))
    events = EventBus()
    observed: list[Event] = []

    async def capture(event: Event) -> None:
        observed.append(event)

    events.subscribe("*", capture)
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"fs.read"})),
        events,
    )

    result = await runtime.execute(ToolCall("1", "read_file", {"path": "hello.txt"}), "run", 1)

    assert result.status is ToolStatus.SUCCEEDED
    assert result.result == {
        "path": "hello.txt",
        "start_line": 1,
        "end_line": 1,
        "content": "hello world",
        "truncated": False,
    }
    assert [event.name for event in observed] == [
        "permission_decided",
        "tool_call_started",
        "tool_call_completed",
    ]


@pytest.mark.asyncio
async def test_missing_optional_file_is_distinct_from_a_path_violation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(WorkspaceSandbox(tmp_path)))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"fs.read"})), EventBus())
    observation = await runtime.execute(
        ToolCall("missing", "read_file", {"path": "AGENTS.md"}), "run", 0
    )
    assert observation.status is ToolStatus.FAILED
    assert observation.error is not None
    assert observation.error.code == "file_not_found"
    assert not observation.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../missing-directory/AGENTS.md", "link/AGENTS.md"])
async def test_missing_external_file_remains_a_path_violation(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    registry = ToolRegistry()
    registry.register(ReadFileTool(WorkspaceSandbox(workspace)))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"fs.read"})), EventBus())
    observation = await runtime.execute(ToolCall("outside", "read_file", {"path": path}), "run", 0)
    assert observation.status is ToolStatus.FAILED
    assert observation.error is not None
    assert observation.error.code == "sandbox_violation"


@pytest.mark.asyncio
async def test_tool_runtime_denies_missing_permission(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(PatchFileTool(WorkspaceSandbox(tmp_path)))
    runtime = ToolRuntime(registry, PermissionPolicy(), EventBus())

    result = await runtime.execute(
        ToolCall("1", "patch_file", {"path": "x", "expected": "a", "replacement": "b"}),
        "run",
        1,
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "permission_denied"


@pytest.mark.asyncio
async def test_shell_uses_argv_and_allowlist(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        WorkspaceSandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({"git"}))
    )
    registry = ToolRegistry()
    registry.register(ShellTool(runner))
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"process.exec"})),
        EventBus(),
    )

    result = await runtime.execute(ToolCall("1", "shell", {"argv": ["rm", "-rf", "."]}), "run", 1)

    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "command_denied"


class RecordingOpener:
    argv: tuple[str, ...] | None = None

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        self.argv = argv
        return ProcessResult(argv, 0, "", "")


@pytest.mark.asyncio
async def test_open_file_opens_a_confined_workspace_path(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    runner = RecordingOpener()
    tool = OpenFileTool(WorkspaceSandbox(tmp_path), runner, "open")

    result = await tool.execute(OpenFileInput(path="index.html"))

    assert result == {"path": "index.html", "opened": True}
    assert runner.argv == ("open", str((tmp_path / "index.html").resolve()))


@pytest.mark.asyncio
async def test_open_file_rejects_escapes_missing_files_and_sensitive_paths(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside.html"
    outside.write_text("secret")
    (tmp_path / ".env").write_text("TOKEN=secret")
    tool = OpenFileTool(WorkspaceSandbox(tmp_path), RecordingOpener(), "open")

    with pytest.raises(ToolError, match="escapes workspace"):
        await tool.execute(OpenFileInput(path="../outside.html"))
    with pytest.raises(ToolError, match="file not found"):
        await tool.execute(OpenFileInput(path="missing.html"))
    with pytest.raises(ToolError, match="sensitive file"):
        await tool.execute(OpenFileInput(path=".env"))


@pytest.mark.asyncio
async def test_a_timed_out_process_is_not_reported_as_success(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        WorkspaceSandbox(tmp_path),
        CommandPolicy(allowed_commands=frozenset({"python3"})),
    )
    registry = ToolRegistry()
    registry.register(ShellTool(runner))
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"process.exec"})),
        EventBus(),
    )

    observation = await runtime.execute(
        ToolCall(
            "1",
            "shell",
            {"argv": ["python3", "-c", "import time; time.sleep(5)"], "timeout_seconds": 0.1},
        ),
        "run",
        1,
    )

    assert observation.status is ToolStatus.TIMED_OUT
    assert isinstance(observation.result, dict)
    assert observation.result["timed_out"] is True


@pytest.mark.asyncio
async def test_docker_runner_disables_network_and_drops_capabilities(tmp_path: Path) -> None:
    class RecordingRunner:
        argv: tuple[str, ...] = ()

        async def run(
            self,
            argv: tuple[str, ...],
            *,
            cwd: str = ".",
            timeout_seconds: float = 30.0,
        ) -> ProcessResult:
            del cwd, timeout_seconds
            self.argv = argv
            return ProcessResult(argv, 0, "ok", "")

    host = RecordingRunner()
    runner = DockerCommandRunner(
        WorkspaceSandbox(tmp_path),
        CommandPolicy(allowed_commands=frozenset({"python"})),
        host,
        image="python:3.12-slim",
    )

    result = await runner.run(("python", "-V"))

    assert result.argv == ("python", "-V")
    assert "none" in host.argv
    assert "ALL" in host.argv
    assert "no-new-privileges" in host.argv


@pytest.mark.asyncio
async def test_explicit_tool_denial_wins_over_a_granted_capability(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(WriteFileTool(WorkspaceSandbox(tmp_path)))
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"fs.write"}), frozenset({"write_file"})),
        EventBus(),
    )

    result = await runtime.execute(
        ToolCall("1", "write_file", {"path": "x.txt", "content": "payload"}), "run", 1
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert not (tmp_path / "x.txt").exists()

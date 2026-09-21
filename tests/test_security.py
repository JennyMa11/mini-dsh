"""Adversarial tests for the sandbox, permission, and process boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from minidsh.adapters import JsonlTraceWriter
from minidsh.domain import Event, ToolCall, ToolStatus
from minidsh.infrastructure import (
    CommandDeniedError,
    CommandPolicy,
    LocalCommandRunner,
    NetworkPolicy,
    NetworkRule,
    SandboxViolationError,
    WorkspaceSandbox,
)
from minidsh.runtime import EventBus
from minidsh.tools import (
    PermissionPolicy,
    ReadFileTool,
    SearchTool,
    ShellTool,
    Tool,
    ToolRegistry,
    ToolRuntime,
    WriteFileTool,
)

_PYTHON = Path(sys.executable).name


def _sandbox(tmp_path: Path) -> WorkspaceSandbox:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WorkspaceSandbox(workspace)


def _runtime(tmp_path: Path, *tools: Tool, permissions: frozenset[str]) -> ToolRuntime:
    del tmp_path
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolRuntime(registry, PermissionPolicy(permissions), EventBus())


# --- 23. path traversal -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "../../etc/passwd",
        "nested/../../secret.txt",
        "./../secret.txt",
        "..",
        "a/b/../../../secret.txt",
    ],
)
def test_parent_traversal_is_rejected(tmp_path: Path, path: str) -> None:
    sandbox = _sandbox(tmp_path)

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        sandbox.resolve(path)


@pytest.mark.parametrize("path", ["/etc/passwd", "/tmp", "/Users"])
def test_absolute_paths_are_rejected(tmp_path: Path, path: str) -> None:
    sandbox = _sandbox(tmp_path)

    with pytest.raises(SandboxViolationError, match="absolute paths"):
        sandbox.resolve(path)


def test_home_directory_is_never_expanded(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    resolved = sandbox.resolve("~/secret")

    assert resolved == sandbox.root / "~" / "secret"
    assert Path.home() not in resolved.parents


@pytest.mark.asyncio
async def test_read_tool_cannot_traverse_out_of_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("classified\n", encoding="utf-8")
    sandbox = _sandbox(tmp_path)
    runtime = _runtime(tmp_path, ReadFileTool(sandbox), permissions=frozenset({"fs.read"}))

    result = await runtime.execute(ToolCall("1", "read_file", {"path": "../outside.txt"}), "run", 1)

    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "sandbox_violation"
    assert "classified" not in json.dumps(result.result)


# --- 24. symlink escape -------------------------------------------------------


def test_symlinked_directory_escape_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    sandbox = WorkspaceSandbox(workspace)

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        sandbox.read_text("escape/secret.txt")


def test_symlinked_file_escape_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("classified\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(target)
    sandbox = WorkspaceSandbox(workspace)

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        sandbox.read_text("link.txt")


def test_atomic_write_through_a_symlinked_parent_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    sandbox = WorkspaceSandbox(workspace)

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        sandbox.atomic_write("escape/new.txt", "payload")

    assert not (outside / "new.txt").exists()


@pytest.mark.asyncio
async def test_write_tool_cannot_escape_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    runtime = _runtime(
        tmp_path, WriteFileTool(WorkspaceSandbox(workspace)), permissions=frozenset({"fs.write"})
    )

    result = await runtime.execute(
        ToolCall("1", "write_file", {"path": "../outside.txt", "content": "payload"}),
        "run",
        1,
    )

    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "sandbox_violation"
    assert not outside.exists()


# --- 25. shell injection ------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_metacharacters_are_passed_literally(tmp_path: Path) -> None:
    marker = tmp_path / "injected.txt"
    payload = f"value; touch {marker}"
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    result = await runner.run((sys.executable, "-c", "import sys; print(sys.argv[1])", payload))

    assert result.exit_code == 0
    assert payload in result.stdout
    assert not marker.exists()


@pytest.mark.asyncio
async def test_shell_tool_reports_injection_attempt_as_a_denied_command(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )
    runtime = _runtime(tmp_path, ShellTool(runner), permissions=frozenset({"process.exec"}))

    result = await runtime.execute(
        ToolCall("1", "shell", {"argv": ["sh", "-c", "rm -rf /tmp"]}), "run", 1
    )

    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "command_denied"


def test_shell_and_interpreters_are_not_implicitly_allowlisted() -> None:
    policy = CommandPolicy(allowed_commands=frozenset({"git"}))

    for argv in (("sh", "-c", "true"), ("bash", "-c", "true"), ("python", "-c", "pass")):
        with pytest.raises(CommandDeniedError, match="allowlisted"):
            policy.validate(argv)


# --- network default deny -----------------------------------------------------


def test_network_commands_are_denied_until_a_rule_exists() -> None:
    denied = CommandPolicy(allowed_commands=frozenset({"curl"}))
    with pytest.raises(CommandDeniedError, match="network command is disabled"):
        denied.validate(("curl", "https://example.com"))

    allowed = CommandPolicy(
        allowed_commands=frozenset({"curl"}),
        network=NetworkPolicy(frozenset({NetworkRule("example.com", "tcp", frozenset({443}))})),
    )
    allowed.validate(("curl", "https://example.com"))


def test_network_rule_validation_and_matching() -> None:
    rule = NetworkRule("pypi.org", "tcp", frozenset({443}))

    assert rule.authorizes("pypi.org", protocol="tcp", port=443) is True
    assert rule.authorizes("pypi.org", protocol="tcp", port=80) is False
    assert rule.authorizes("other.org", protocol="tcp", port=443) is False

    wildcard = NetworkRule("*")
    assert wildcard.authorizes("anything.example", protocol="tcp", port=22) is True

    with pytest.raises(ValueError, match="protocol"):
        NetworkRule("pypi.org", "icmp")
    with pytest.raises(ValueError, match="ports"):
        NetworkRule("pypi.org", "tcp", frozenset({0}))
    with pytest.raises(ValueError, match="host"):
        NetworkRule("  ")


def test_network_policy_is_deny_all_by_default() -> None:
    policy = NetworkPolicy.deny_all()

    assert policy.enabled is False
    assert policy.authorizes("example.com", protocol="tcp", port=443) is False


# --- 26. secret leakage -------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [".env", ".env.production", "id_rsa", "credentials.json", "server.pem", ".ssh/config"],
)
def test_sensitive_files_are_never_readable(tmp_path: Path, relative: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("super-secret-value\n", encoding="utf-8")
    sandbox = WorkspaceSandbox(workspace)

    with pytest.raises(SandboxViolationError) as excinfo:
        sandbox.read_text(relative)

    assert "sensitive file access denied" in str(excinfo.value)
    assert "super-secret-value" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_skips_sensitive_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=super-secret-value\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("super-secret-value\n", encoding="utf-8")
    runtime = _runtime(
        tmp_path, SearchTool(WorkspaceSandbox(workspace)), permissions=frozenset({"fs.read"})
    )

    result = await runtime.execute(
        ToolCall("1", "search", {"query": "super-secret-value"}), "run", 1
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert result.result == {
        "matches": [{"path": "notes.txt", "line": 1, "text": "super-secret-value"}],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_subprocess_receives_only_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIDSH_TEST_SECRET", "super-secret-value")
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import os; print(os.environ.get('MINIDSH_TEST_SECRET', '<absent>'))",
        )
    )

    assert result.stdout.strip() == "<absent>"
    assert "super-secret-value" not in result.stdout


@pytest.mark.asyncio
async def test_trace_redacts_secrets(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    writer = JsonlTraceWriter(trace)

    await writer.append(
        Event(
            "model_call_started",
            "run-1",
            1,
            {
                "api_key": "super-secret-value",
                "headers": {"Authorization": "Bearer super-secret-value"},
                "note": "password=super-secret-value",
            },
        )
    )

    written = trace.read_text(encoding="utf-8")

    assert "super-secret-value" not in written
    assert "[REDACTED]" in written
    assert json.loads(written)["schema_version"] == "1.0"


# --- 27. subprocess timeout and process-group cleanup -------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_kills_the_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "orphan.txt"
    grandchild = (
        f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('orphan')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(30)"
    )
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    result = await runner.run((sys.executable, "-c", parent), timeout_seconds=0.5)

    assert result.timed_out is True
    assert result.exit_code < 0 or result.exit_code != 0
    await asyncio.sleep(2.0)
    assert not marker.exists(), "the grandchild survived the process-group kill"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_is_reported_without_hanging(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    result = await runner.run(
        (sys.executable, "-c", "import time; time.sleep(30)"), timeout_seconds=0.4
    )

    assert result.timed_out is True
    assert result.argv == (sys.executable, "-c", "import time; time.sleep(30)")


# --- command policy hardening -------------------------------------------------


def test_denied_commands_win_over_the_allowlist() -> None:
    policy = CommandPolicy(allowed_commands=frozenset({"git", "rm", "sudo"}))

    for argv in (("rm", "-rf", "."), ("sudo", "ls"), ("ssh", "host")):
        with pytest.raises(CommandDeniedError, match="denied"):
            policy.validate(argv)


def test_empty_argv_is_rejected() -> None:
    policy = CommandPolicy(allowed_commands=frozenset({"git"}))

    with pytest.raises(CommandDeniedError, match="cannot be empty"):
        policy.validate(())


@pytest.mark.asyncio
async def test_runner_refuses_a_working_directory_outside_the_workspace(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    with pytest.raises(SandboxViolationError, match="escapes workspace"):
        await runner.run((sys.executable, "-c", "pass"), cwd="../")


@pytest.mark.asyncio
async def test_runner_refuses_a_missing_working_directory(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    with pytest.raises(SandboxViolationError, match="cannot resolve path"):
        await runner.run((sys.executable, "-c", "pass"), cwd="does-not-exist")

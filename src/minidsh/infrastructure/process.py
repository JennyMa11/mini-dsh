"""Argument-array-only process execution with policy and timeout controls."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from minidsh.infrastructure.sandbox import WorkspaceSandbox

_DEFAULT_DENIED = frozenset(
    {
        "rm",
        "scp",
        "ssh",
        "sudo",
    }
)
_NETWORK_COMMANDS = frozenset({"curl", "ftp", "nc", "netcat", "telnet", "wget"})
_DEFAULT_ENV_KEYS = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ"})
_ALLOWED_PROTOCOLS = frozenset({"tcp", "udp"})
_MAX_PORT = 65_535


class CommandDeniedError(PermissionError):
    """Raised before spawning a command rejected by policy."""


@dataclass(frozen=True, slots=True)
class NetworkRule:
    """One explicitly authorized network destination."""

    host: str
    protocol: str = "tcp"
    ports: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not self.host or any(character.isspace() for character in self.host):
            msg = "network rule host must be a non-empty token without whitespace"
            raise ValueError(msg)
        if self.protocol not in _ALLOWED_PROTOCOLS:
            msg = f"network protocol must be one of {sorted(_ALLOWED_PROTOCOLS)}"
            raise ValueError(msg)
        if any(port < 1 or port > _MAX_PORT for port in self.ports):
            msg = "network ports must be within 1-65535"
            raise ValueError(msg)

    def authorizes(self, host: str, *, protocol: str, port: int | None) -> bool:
        """Return whether this rule covers one destination triple."""
        if self.host not in {host, "*"} or self.protocol != protocol:
            return False
        return not self.ports or port is None or port in self.ports


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Default-deny network authorization expressed as host/protocol/port rules."""

    allowed: frozenset[NetworkRule] = frozenset()

    @property
    def enabled(self) -> bool:
        """Return whether any destination is authorized."""
        return bool(self.allowed)

    def authorizes(self, host: str, *, protocol: str = "tcp", port: int | None = None) -> bool:
        """Return whether the destination is covered by an explicit rule."""
        return any(rule.authorizes(host, protocol=protocol, port=port) for rule in self.allowed)

    @classmethod
    def deny_all(cls) -> NetworkPolicy:
        """Return the default deny-everything policy."""
        return cls()


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Deny-first executable allowlist plus a default-deny network policy."""

    allowed_commands: frozenset[str] = field(default_factory=frozenset)
    denied_commands: frozenset[str] = _DEFAULT_DENIED
    network: NetworkPolicy = field(default_factory=NetworkPolicy)

    def validate(self, argv: tuple[str, ...]) -> None:
        """Validate an executable name; arguments never pass through a shell."""
        if not argv or not argv[0].strip():
            msg = "command argv cannot be empty"
            raise CommandDeniedError(msg)
        command = Path(argv[0]).name
        if command in self.denied_commands:
            msg = f"command is denied: {command}"
            raise CommandDeniedError(msg)
        if not self.network.enabled and command in _NETWORK_COMMANDS:
            msg = f"network command is disabled: {command}"
            raise CommandDeniedError(msg)
        if command not in self.allowed_commands:
            msg = f"command is not allowlisted: {command}"
            raise CommandDeniedError(msg)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded process outcome."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class ProcessRunner(Protocol):
    """Replaceable local or hardened argv execution adapter."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        """Execute one argv array and return a normalized result."""


class LocalCommandRunner:
    """Run a child process in the workspace without invoking a shell."""

    def __init__(
        self,
        sandbox: WorkspaceSandbox,
        policy: CommandPolicy,
        *,
        environment_keys: frozenset[str] = _DEFAULT_ENV_KEYS,
    ) -> None:
        self._sandbox = sandbox
        self._policy = policy
        self._environment_keys = environment_keys

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        """Spawn a process and kill its process group if its deadline expires."""
        self._policy.validate(argv)
        working_directory = self._sandbox.resolve(cwd, must_exist=True)
        if not working_directory.is_dir():
            msg = f"working directory is not a directory: {cwd}"
            raise CommandDeniedError(msg)
        environment = {
            key: value for key, value in os.environ.items() if key in self._environment_keys
        }
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_directory,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return ProcessResult(
                argv,
                process.returncode if process.returncode is not None else -1,
                stdout_bytes.decode(errors="replace"),
                stderr_bytes.decode(errors="replace"),
                timed_out=True,
            )
        return ProcessResult(
            argv,
            process.returncode if process.returncode is not None else -1,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )


class DockerCommandRunner:
    """Run allowlisted commands in a capability-dropped, network-controlled container."""

    def __init__(
        self,
        sandbox: WorkspaceSandbox,
        policy: CommandPolicy,
        host_runner: ProcessRunner,
        *,
        image: str,
        network: NetworkPolicy | None = None,
    ) -> None:
        if not image or any(character.isspace() for character in image):
            msg = "docker image must be a non-empty reference without whitespace"
            raise ValueError(msg)
        self._sandbox = sandbox
        self._policy = policy
        self._host_runner = host_runner
        self._image = image
        self._network = network or policy.network

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        """Translate a workspace command into a constrained ``docker run`` invocation."""
        self._policy.validate(argv)
        resolved_cwd = self._sandbox.resolve(cwd, must_exist=True)
        relative_cwd = resolved_cwd.relative_to(self._sandbox.root).as_posix()
        container_cwd = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
        network = "bridge" if self._network.enabled else "none"
        docker_argv = (
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--volume",
            f"{self._sandbox.root}:/workspace:rw",
            "--workdir",
            container_cwd,
            self._image,
            *argv,
        )
        outer = await self._host_runner.run(
            docker_argv,
            timeout_seconds=timeout_seconds + 10,
        )
        return ProcessResult(argv, outer.exit_code, outer.stdout, outer.stderr, outer.timed_out)

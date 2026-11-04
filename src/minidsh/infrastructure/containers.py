"""Persistent, mount-free Docker isolation with bounded host process output."""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType

from minidsh.infrastructure.process import (
    CommandDeniedError,
    CommandPolicy,
    ProcessResult,
    ProcessRunner,
)

_OUTPUT_LIMIT = 8 * 1024 * 1024
_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONTEXT"})


def _write_binary_capture(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


@dataclass(frozen=True, slots=True)
class ContainerResources:
    """Explicit platform and bounded per-container resources."""

    platform: str = "linux/amd64"
    memory_mb: int = 4096
    cpus: int = 2
    pids_limit: int = 512

    def __post_init__(self) -> None:
        if self.platform not in {"linux/amd64", "linux/arm64"}:
            raise ValueError("unsupported container platform")
        for value, maximum in [(self.memory_mb, 4096), (self.cpus, 8), (self.pids_limit, 512)]:
            if type(value) is not int or value <= 0 or value > maximum:
                raise ValueError("invalid container resource limit")


class DockerHost:
    """Trusted Docker driver; credentials are never forwarded to containers."""

    def __init__(self, *, executable: str | None = None) -> None:
        desktop = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
        self._executable = (
            executable
            or shutil.which("docker")
            or (str(desktop) if desktop.is_file() else "docker")
        )

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30,
        binary_output: Path | None = None,
    ) -> ProcessResult:
        """Drain bounded pipes, killing the client process group on interruption."""
        if not argv or argv[0] != "docker":
            raise CommandDeniedError("DockerHost only accepts Docker commands")
        if binary_output is not None and (
            await asyncio.to_thread(binary_output.exists)
            or await asyncio.to_thread(binary_output.is_symlink)
        ):
            raise FileExistsError("binary_capture_already_exists")
        environment = {key: value for key, value in os.environ.items() if key in _ENV_KEYS}
        executable_path = Path(self._executable)
        if executable_path.is_absolute():
            environment["PATH"] = (
                str(executable_path.parent) + os.pathsep + environment.get("PATH", "")
            )
        process = await asyncio.create_subprocess_exec(
            self._executable,
            *argv[1:],
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        truncated = False

        async def drain(stream: asyncio.StreamReader | None) -> bytes:
            nonlocal truncated
            output = bytearray()
            if stream is not None:
                while chunk := await stream.read(65_536):
                    remaining = _OUTPUT_LIMIT - len(output)
                    output.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
            return bytes(output)

        stdout_task = asyncio.create_task(drain(process.stdout))
        stderr_task = asyncio.create_task(drain(process.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                await asyncio.gather(stdout_task, stderr_task)
                raise
            timed_out = True
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        if (
            binary_output is not None
            and not truncated
            and not timed_out
            and process.returncode == 0
        ):
            await asyncio.to_thread(_write_binary_capture, binary_output, stdout)
        detail = stderr.decode(errors="replace")
        if truncated:
            detail += "\n[output truncated at 8 MiB per stream]"
        return ProcessResult(
            argv,
            125 if truncated else (process.returncode or 0),
            stdout.decode(errors="replace") if binary_output is None else "",
            detail,
            timed_out,
        )


class DockerSandbox:
    """One disposable Linux container; host paths and sockets are never mounted."""

    def __init__(
        self, host: ProcessRunner, *, image: str, resources: ContainerResources | None = None
    ) -> None:
        if re.fullmatch(r"[a-zA-Z0-9._/:-]+@sha256:[a-f0-9]{64}", image) is None:
            raise ValueError("container image must be pinned by SHA-256 digest")
        self.host = host
        self.image = image
        self.resources = resources or ContainerResources()
        self.name = "minidsh-" + uuid.uuid4().hex
        self.alive = False

    async def __aenter__(self) -> DockerSandbox:
        try:
            result = await self.host.run(
                (
                    "docker",
                    "run",
                    "--detach",
                    "--pull",
                    "never",
                    "--name",
                    self.name,
                    "--platform",
                    self.resources.platform,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    str(self.resources.pids_limit),
                    "--memory",
                    str(self.resources.memory_mb) + "m",
                    "--cpus",
                    str(self.resources.cpus),
                    "--entrypoint",
                    "/bin/sleep",
                    self.image,
                    "infinity",
                ),
                timeout_seconds=120,
            )
            if result.exit_code or result.timed_out:
                raise OSError("container creation failed")
            self.alive = True
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Remove only this owned container, including all its child processes."""
        self.alive = False
        result = await self.host.run(("docker", "rm", "--force", self.name))
        if result.exit_code and "No such container" not in result.stderr:
            raise OSError("container cleanup failed")

    async def kill(self) -> None:
        """Stop all processes after a deadline or cancellation."""
        self.alive = False
        result = await self.host.run(("docker", "kill", self.name))
        if result.exit_code:
            raise OSError("container termination failed")

    async def execute(
        self, argv: tuple[str, ...], *, cwd: str = "/", timeout_seconds: float = 30
    ) -> ProcessResult:
        """Trusted orchestration command; never concatenate model text into shell code."""
        if not self.alive:
            raise CommandDeniedError("container is no longer running")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        try:
            result = await self.host.run(
                (
                    "docker",
                    "exec",
                    "--workdir",
                    cwd,
                    self.name,
                    "/usr/bin/timeout",
                    "--signal=KILL",
                    str(timeout_seconds),
                    *argv,
                ),
                timeout_seconds=timeout_seconds + 10,
            )
        except asyncio.CancelledError:
            await self.kill()
            raise
        if result.timed_out or result.exit_code in {124, 137}:
            await self.kill()
            return ProcessResult(
                argv, result.exit_code, result.stdout, result.stderr, timed_out=True
            )
        return ProcessResult(argv, result.exit_code, result.stdout, result.stderr)

    def runner(self, repository: str, policy: CommandPolicy) -> ContainerCommandRunner:
        """Expose an argv-only runner with an explicit command policy."""
        return ContainerCommandRunner(self, repository, policy)


class ContainerCommandRunner:
    """Model-facing command port, confined to one container repository."""

    def __init__(self, box: DockerSandbox, repository: str, policy: CommandPolicy) -> None:
        if repository not in {"/app", "/testbed"}:
            raise ValueError("unsupported container repository")
        self._box = box
        self._repository = repository
        self._policy = policy

    async def run(
        self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
    ) -> ProcessResult:
        """Validate model arguments before translating them to Docker exec."""
        self._policy.validate(argv)
        path = PurePosixPath(cwd)
        if path.is_absolute() or ".." in path.parts or "\x00" in cwd:
            raise CommandDeniedError("cwd must remain relative to the repository")
        directory = str(PurePosixPath(self._repository) / path)
        return await self._box.execute(argv, cwd=directory, timeout_seconds=timeout_seconds)

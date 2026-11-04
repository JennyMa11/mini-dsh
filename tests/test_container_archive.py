"""Bounded binary Docker transport, without requiring a Docker daemon."""

import sys
from pathlib import Path

import pytest

from minidsh.infrastructure.containers import DockerHost

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_binary_capture_preserves_invalid_utf8_and_never_overwrites(tmp_path: Path) -> None:
    executable = tmp_path / "docker-fixture"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'raw\\x00\\xffarchive')\n"
    )
    executable.chmod(0o700)
    host = DockerHost(executable=str(executable))
    target = tmp_path / "candidate.tar"
    result = await host.run(("docker", "cp", "owned:/app/.", "-"), binary_output=target)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert target.read_bytes() == b"raw\x00\xffarchive"
    with pytest.raises(FileExistsError):
        await host.run(("docker", "cp", "owned:/app/.", "-"), binary_output=target)
    assert target.read_bytes() == b"raw\x00\xffarchive"


@pytest.mark.parametrize("failure", ["nonzero", "oversize"])
@pytest.mark.asyncio
async def test_failed_binary_capture_never_publishes_a_partial_archive(
    tmp_path: Path, failure: str
) -> None:
    executable = tmp_path / "docker-fixture"
    program = (
        "raise SystemExit(3)"
        if failure == "nonzero"
        else "sys.stdout.buffer.write(b'x' * (9 * 1024 * 1024))"
    )
    executable.write_text(f"#!{sys.executable}\nimport sys\n{program}\n")
    executable.chmod(0o700)
    target = tmp_path / "candidate.tar"
    result = await DockerHost(executable=str(executable)).run(
        ("docker", "cp", "owned:/app/.", "-"), binary_output=target
    )
    assert result.exit_code == (3 if failure == "nonzero" else 125)
    assert result.stdout == ""
    assert not target.exists()

"""Contracts for the public benchmark's isolated execution boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from minidsh.adapters.models import ScriptedModel
from minidsh.benchmark.public_swe import (
    PublicSelection,
    PublicStudyError,
    _capture_patch,
    _fingerprint,
    _tree_hash,
    resolve_image,
    run_study,
    summarize,
)
from minidsh.domain import ModelResponse, TokenUsage
from minidsh.infrastructure.containers import DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandDeniedError, CommandPolicy, ProcessResult


class RecordingHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeout = False
        self.cancel = False

    async def run(
        self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
    ) -> ProcessResult:
        del cwd, timeout_seconds
        self.calls.append(argv)
        if argv[1] == "exec" and self.cancel:
            raise asyncio.CancelledError
        return ProcessResult(argv, 0, "ok", "", self.timeout and argv[1] == "exec")


@pytest.mark.asyncio
async def test_docker_driver_finds_bundled_credential_helper_without_forwarding_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment: dict[str, str] = {}

    class Process:
        returncode = 0
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()

        async def wait(self) -> int:
            return 0

    async def spawn(*args: object, **kwargs: object) -> Process:
        del args
        env = kwargs["env"]
        assert isinstance(env, dict)
        environment.update(env)
        process = Process()
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        return process

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-private-token")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    host = DockerHost(executable="/Applications/Docker.app/Contents/Resources/bin/docker")
    await host.run(("docker", "version"))
    assert environment["PATH"].split(":")[0] == "/Applications/Docker.app/Contents/Resources/bin"
    assert "DEEPSEEK_API_KEY" not in environment


@pytest.mark.parametrize("mirror_available", [True, False])
@pytest.mark.parametrize("manifest_index", [True, False])
@pytest.mark.asyncio
async def test_domestic_mirror_uses_upstream_digest_and_falls_back_without_changing_it(
    *, mirror_available: bool, manifest_index: bool
) -> None:
    task = PublicSelection.from_file(Path("benchmarks/public_swe/v1/selection.json")).tasks[0]
    digest = "sha256:" + "c" * 64

    class RegistryHost(RecordingHost):
        def __init__(self) -> None:
            super().__init__()
            self.pulled_refs: set[str] = set()

        async def run(
            self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
        ) -> ProcessResult:
            del cwd
            self.calls.append(argv)
            if argv[1] == "manifest":
                manifest = {
                    "Descriptor": {
                        "digest": digest,
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                }
                attestation = {
                    "Descriptor": {
                        "digest": "sha256:" + "d" * 64,
                        "platform": {"architecture": "unknown", "os": "unknown"},
                    }
                }
                return ProcessResult(
                    argv,
                    0,
                    json.dumps([attestation, manifest] if manifest_index else manifest),
                    "",
                )
            if argv[1] == "pull":
                assert timeout_seconds == 1800
                failed = argv[-1].startswith("ghcr.1ms.run/") and not mirror_available
                if not failed:
                    self.pulled_refs.add(argv[-1])
                return ProcessResult(argv, int(failed), "", "mirror unavailable" if failed else "")
            if argv[1:3] == ("image", "inspect"):
                if argv[-1] not in self.pulled_refs:
                    return ProcessResult(argv, 1, "", "not cached")
                return ProcessResult(
                    argv,
                    0,
                    json.dumps(
                        [{"Architecture": "amd64", "Os": "linux", "RepoDigests": [argv[-1]]}]
                    ),
                    "",
                )
            pytest.fail("unexpected registry command")

    host = RegistryHost()
    reference = await resolve_image(host, task, mirror="ghcr.1ms.run")
    expected_host = "ghcr.1ms.run" if mirror_available else "ghcr.io"
    assert reference == expected_host + "/scaleapi/swe-bench_pro-v2@" + digest
    pulls = [argv for argv in host.calls if argv[1] == "pull"]
    assert all(argv[-1].endswith("@" + digest) for argv in pulls)
    assert len(pulls) == (1 if mirror_available else 2)


@pytest.mark.asyncio
async def test_registry_mirror_rejects_credentials_paths_and_insecure_endpoints() -> None:
    task = PublicSelection.from_file(Path("benchmarks/public_swe/v1/selection.json")).tasks[0]
    for mirror in ["http://example.com", "user:password@example.com", "example.com/path"]:
        with pytest.raises(PublicStudyError, match="mirror"):
            await resolve_image(RecordingHost(), task, mirror=mirror)


@pytest.mark.asyncio
async def test_container_has_no_host_mounts_secrets_or_network() -> None:
    host = RecordingHost()
    box = DockerSandbox(host, image="example@sha256:" + "a" * 64)
    async with box:
        command = host.calls[0]
        assert command[command.index("--network") + 1] == "none"
        assert command[command.index("--platform") + 1] == "linux/amd64"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert not {"--volume", "-v", "--env", "-e", "--privileged"} & set(command)
    assert host.calls[-1][:3] == ("docker", "rm", "--force")


@pytest.mark.asyncio
async def test_model_argv_is_not_interpolated_into_a_shell() -> None:
    host = RecordingHost()
    async with DockerSandbox(host, image="example@sha256:" + "a" * 64) as box:
        runner = box.runner("/app", CommandPolicy(frozenset({"python3"})))
        payload = "print('$(touch /host); `id`')"
        await runner.run(("python3", "-c", payload), cwd="package")
        command = host.calls[-1]
        assert command[-3:] == ("python3", "-c", payload)
        assert command[command.index("--workdir") + 1] == "/app/package"


@pytest.mark.parametrize("cwd", ["../outside", "/", "a/../../outside"])
@pytest.mark.asyncio
async def test_command_workdir_cannot_escape_repository(cwd: str) -> None:
    host = RecordingHost()
    async with DockerSandbox(host, image="example@sha256:" + "a" * 64) as box:
        runner = box.runner("/app", CommandPolicy(frozenset({"python3"})))
        with pytest.raises(CommandDeniedError):
            await runner.run(("python3", "-V"), cwd=cwd)
        assert len(host.calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_timeout_and_cancel_kill_entire_container(*, cancel: bool) -> None:
    host = RecordingHost()
    async with DockerSandbox(host, image="example@sha256:" + "a" * 64) as box:
        host.cancel = cancel
        host.timeout = not cancel
        runner = box.runner("/app", CommandPolicy(frozenset({"python3"})))
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await runner.run(("python3", "-V"))
        else:
            assert (await runner.run(("python3", "-V"))).timed_out
        assert host.calls[-1][:2] == ("docker", "kill")
        with pytest.raises(CommandDeniedError):
            await runner.run(("python3", "-V"))


def test_selection_is_frozen_validated_and_retains_all_tasks() -> None:
    selection = PublicSelection.from_file(Path("benchmarks/public_swe/v1/selection.json"))
    assert len(selection.tasks) == 12
    assert len({task.repository for task in selection.tasks}) == 11
    assert selection.token_budget == 900_000


def test_selection_rejects_path_traversal_and_unknown_protocol(tmp_path: Path) -> None:
    path = Path("benchmarks/public_swe/v1/selection.json")
    payload = json.loads(path.read_text())
    payload["tasks"][0]["upstream_task_path"] = "../../secret"
    target = tmp_path / "selection.json"
    target.write_text(json.dumps(payload))
    with pytest.raises(PublicStudyError):
        PublicSelection.from_file(target)
    payload = json.loads(path.read_text())
    payload["run_protocol"]["repeats"] = 3
    target.write_text(json.dumps(payload))
    with pytest.raises(PublicStudyError):
        PublicSelection.from_file(target)


def test_report_counts_failures_and_unrun_tasks_in_denominator() -> None:
    rows: list[dict[str, object]] = [
        {"success": True, "tokens": 20, "status": "finished"},
        {"success": False, "tokens": 30, "status": "budget_exhausted"},
        {"success": False, "tokens": 0, "status": "not_run"},
    ]
    report = summarize(rows, 45)
    assert report["success_rate"] == pytest.approx(1 / 3)
    assert report["tokens_per_success"] == 50
    assert report["token_overrun"] == 5
    assert "cost_usd" not in report


def test_source_tampering_is_rejected_before_execution(tmp_path: Path) -> None:
    selection = PublicSelection.from_file(Path("benchmarks/public_swe/v1/selection.json"))
    task = selection.tasks[0]
    root = tmp_path / task.upstream_id
    root.mkdir()
    (root / "instruction.md").write_text("changed")
    expected = hashlib.sha256(b"changed").hexdigest()
    assert "sha256:" + expected != task.source_file_sha256["instruction.md"]
    with pytest.raises(PublicStudyError, match="source"):
        selection.verify_sources(tmp_path)


def _synthetic_sources(tmp_path: Path) -> PublicSelection:
    payload = json.loads(Path("benchmarks/public_swe/v1/selection.json").read_text())
    payload["run_protocol"]["total_run_token_budget"] = 10
    for task in payload["tasks"]:
        directory = tmp_path / "sources" / task["upstream_id"]
        for filename in task["source_file_sha256"]:
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            body = b"synthetic fixture"
            path.write_bytes(body)
            task["source_file_sha256"][filename] = "sha256:" + hashlib.sha256(body).hexdigest()
        task["upstream_task_tree_git_sha1"] = _tree_hash(directory)
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(payload))
    return PublicSelection.from_file(selection_path)


@pytest.mark.asyncio
async def test_missing_docker_prevents_even_model_construction(tmp_path: Path) -> None:
    selection = _synthetic_sources(tmp_path)

    class MissingHost(RecordingHost):
        async def run(
            self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
        ) -> ProcessResult:
            del argv, cwd, timeout_seconds
            raise FileNotFoundError("docker")

    def forbidden_factory() -> ScriptedModel:
        pytest.fail("model must not be constructed before infrastructure gates")

    with pytest.raises(PublicStudyError, match="Docker is unavailable"):
        await run_study(
            selection,
            sources=tmp_path / "sources",
            output=tmp_path / "output",
            host=MissingHost(),
            model_factory=forbidden_factory,
        )


@pytest.mark.asyncio
async def test_patch_capture_uses_pinned_commit_and_includes_new_files(tmp_path: Path) -> None:
    host = RecordingHost()
    async with DockerSandbox(host, image="example@sha256:" + "a" * 64) as box:
        await _capture_patch(box, "/app", "b" * 40, tmp_path / "patch")
        assert "--intent-to-add" in host.calls[-2]
        assert host.calls[-1][-2:] == ("b" * 40, "--")


@pytest.mark.asyncio
async def test_sweep_stops_after_aggregate_overrun_and_keeps_all_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = _synthetic_sources(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    images = {task.id: "example@sha256:" + "a" * 64 for task in selection.tasks}
    (output / "images.json").write_text(json.dumps(images))
    (output / "gates.json").write_text(
        json.dumps(
            {
                "fingerprint": _fingerprint(selection, images),
                "tasks": [
                    {"task_id": task.id, "reference_passed": True, "empty_passed": False}
                    for task in selection.tasks
                ],
            }
        )
    )
    made = 0

    def factory() -> ScriptedModel:
        nonlocal made
        made += 1
        return ScriptedModel([ModelResponse(content="done", usage=TokenUsage(5, 3))])

    async def repository(box: DockerSandbox, task: object) -> str:
        del box, task
        return "/app"

    async def verify(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr("minidsh.benchmark.public_swe._repository", repository)
    monkeypatch.setattr("minidsh.benchmark.public_swe.verify_patch", verify)
    report = await run_study(
        selection,
        sources=tmp_path / "sources",
        output=output,
        host=RecordingHost(),
        model_factory=factory,
    )
    assert made == 2
    aggregate = report["aggregate"]
    assert isinstance(aggregate, dict)
    assert aggregate["tasks"] == 12
    assert aggregate["total_tokens"] == 16
    assert aggregate["token_overrun"] == 6
    results = report["results"]
    assert isinstance(results, list)
    assert all(row["status"] == "not_run" for row in results[2:])
    assert all(row["failure_type"] == "aggregate_budget_exhausted" for row in results[2:])

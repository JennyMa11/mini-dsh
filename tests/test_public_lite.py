"""Contracts for lightweight, native-architecture public repair evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import TracebackType

import pytest

from minidsh.benchmark import public_lite
from minidsh.benchmark.public_lite import (
    LiteSelection,
    PublicLiteError,
    evaluate_output,
    fixture_files,
)
from minidsh.benchmark.public_swe import UsageModel
from minidsh.domain import ModelCallError, ModelRequest, ModelResponse, TokenUsage
from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import ProcessResult
from minidsh.ports import Model


@pytest.mark.asyncio
async def test_native_container_enforces_small_limits_without_changing_isolation() -> None:
    calls: list[tuple[str, ...]] = []

    class Host:
        async def run(
            self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
        ) -> ProcessResult:
            del cwd, timeout_seconds
            calls.append(argv)
            return ProcessResult(argv, 0, "", "")

    limits = ContainerResources(platform="linux/arm64", memory_mb=1024, cpus=1, pids_limit=128)
    async with DockerSandbox(Host(), image="example@sha256:" + "a" * 64, resources=limits):
        command = calls[0]
        for flag, value in {
            "--platform": "linux/arm64",
            "--memory": "1024m",
            "--cpus": "1",
            "--pids-limit": "128",
            "--network": "none",
            "--cap-drop": "ALL",
            "--pull": "never",
        }.items():
            assert command[command.index(flag) + 1] == value
        assert not {"--volume", "-v", "--env", "-e", "--privileged"} & set(command)


@pytest.mark.parametrize(
    "kwargs",
    [{"platform": "host"}, {"memory_mb": 0}, {"cpus": 0}, {"pids_limit": -1}],
)
def test_container_limits_reject_unsafe_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"unsupported|invalid"):
        ContainerResources(**kwargs)  # type: ignore[arg-type]


def test_public_lite_selection_rejects_heavy_or_duplicate_protocol(tmp_path: Path) -> None:
    frozen = Path("benchmarks/public_lite/v1/selection.json")
    selection = LiteSelection.from_file(frozen)
    assert len(selection.tasks) == 12
    assert selection.run_protocol.total_run_token_budget == 240_000
    for change in ["heavy", "duplicate"]:
        body = json.loads(frozen.read_text())
        if change == "heavy":
            body["run_protocol"]["memory_mb"] = 4096
        else:
            body["tasks"][1] = body["tasks"][0]
        path = tmp_path / "selection.json"
        path.write_text(json.dumps(body))
        with pytest.raises(PublicLiteError):
            LiteSelection.from_file(path)


def test_agent_fixture_contains_only_buggy_code_contract_and_public_example() -> None:
    record = {
        "prompt": "def answer():\n",
        "buggy_solution": "    return 0\n",
        "canonical_solution": "    return 42 # PRIVATE_REFERENCE\n",
        "example_test": "assert answer() == 42\n",
        "test": "assert answer() == 42 # PRIVATE_TEST\n",
        "docstring": "Return 42.",
    }
    files = fixture_files("python", record)
    assert files["solution.py"] == "def answer():\n    return 0\n"
    combined = "\n".join(files.values())
    assert "PRIVATE_REFERENCE" not in combined
    assert "PRIVATE_TEST" not in combined
    assert "assert answer() == 42" in combined


def test_assertion_messages_or_missing_success_marker_cannot_pass() -> None:
    argv = ("node", "verify.js")
    assert not evaluate_output(ProcessResult(argv, 0, "", "Assertion failed"), "NONCE")
    assert not evaluate_output(ProcessResult(argv, 0, "NONCE\n", "Assertion failed"), "NONCE")
    assert not evaluate_output(ProcessResult(argv, 0, "wrong marker\n", ""), "NONCE")
    assert not evaluate_output(ProcessResult(argv, 1, "NONCE\n", ""), "NONCE")
    assert evaluate_output(ProcessResult(argv, 0, "NONCE\n", ""), "NONCE")


@pytest.mark.asyncio
async def test_cancelled_inflight_model_call_is_recorded_as_unknown_usage() -> None:
    started = asyncio.Event()
    blocked = asyncio.Event()

    class PendingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            started.set()
            await blocked.wait()
            return ModelResponse("unreachable")

    model = UsageModel(PendingModel())
    pending = asyncio.create_task(model.complete(ModelRequest(())))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert model.unknown_calls == 1


def _synthetic_selection(root: Path) -> LiteSelection:
    payload = json.loads(Path("benchmarks/public_lite/v1/selection.json").read_text())
    files: dict[str, list[dict[str, str]]] = {"python": [], "js": []}
    for task in payload["tasks"]:
        python = task["language"] == "python"
        entry = task["entry_point"]
        prompt = f"def {entry}():\n" if python else f"const {entry} = () => {{\n"
        buggy = "    return 0\n" if python else "return 0;};\n"
        record = {
            "task_id": task["upstream_id"],
            "prompt": prompt,
            "buggy_solution": buggy,
            "canonical_solution": "PRIVATE_REFERENCE",
            "example_test": "",
            "test": "PRIVATE_TEST",
            "docstring": "Return 42.",
            "entry_point": entry,
            "bug_type": task["bug_type"],
        }
        files[task["language"]].append(record)
        task["record_sha256"] = public_lite._record_digest(record)
        task["initial_file_sha256"] = hashlib.sha256((prompt + buggy).encode()).hexdigest()
    sources = root / "sources"
    sources.mkdir()
    for language, records in files.items():
        body = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
        source = payload["source"]["files"][language]
        (sources / source["filename"]).write_bytes(body)
        source["sha256"] = hashlib.sha256(body).hexdigest()
        source["git_blob_sha1"] = hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()
    path = root / "selection.json"
    path.write_text(json.dumps(payload))
    return LiteSelection.from_file(path)


def _synthetic_gates(selection: LiteSelection, output: Path) -> None:
    images: dict[str, object] = {
        "platform": "linux/arm64",
        "images": {"python": "example@sha256:" + "a" * 64, "js": "example@sha256:" + "b" * 64},
        "size_bytes": {"python": 1, "js": 1},
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "images.json").write_text(json.dumps(images))
    (output / "gates.json").write_text(
        json.dumps(
            {
                "fingerprint": public_lite._fingerprint(selection, images),
                "tasks": [
                    {"task_id": t.id, "reference_passed": True, "buggy_passed": False}
                    for t in selection.tasks
                ],
            }
        ),
    )


class _Host:
    architecture = "aarch64"

    async def run(
        self, argv: tuple[str, ...], *, cwd: str = ".", timeout_seconds: float = 30
    ) -> ProcessResult:
        del cwd, timeout_seconds
        return ProcessResult(argv, 0, self.architecture if argv[1] == "info" else "", "")


@pytest.mark.parametrize("reason", ["missing_gates", "changed_architecture", "changed_source"])
@pytest.mark.asyncio
async def test_invalid_inputs_block_model_construction(tmp_path: Path, reason: str) -> None:
    selection = _synthetic_selection(tmp_path)
    output = tmp_path / "results"
    host = _Host()
    if reason != "missing_gates":
        _synthetic_gates(selection, output)
    if reason == "changed_architecture":
        host.architecture = "amd64"
    elif reason == "changed_source":
        (tmp_path / "sources" / "python.jsonl").write_text("tampered")

    def forbidden_factory() -> Model:
        pytest.fail("credentials and model construction must follow complete valid gates")

    with pytest.raises(PublicLiteError):
        await public_lite.run_study(
            selection,
            sources=tmp_path / "sources",
            output=output,
            host=host,
            model_factory=forbidden_factory,
        )


@pytest.mark.parametrize("unknown_usage", [True, False])
@pytest.mark.asyncio
async def test_sweep_stops_on_unknown_usage_or_aggregate_budget_without_dropping_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unknown_usage: bool
) -> None:
    selection = _synthetic_selection(tmp_path)
    output = tmp_path / "results"
    _synthetic_gates(selection, output)
    calls = 0

    class FakeModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls
            del request
            calls += 1
            if unknown_usage:
                raise ModelCallError("synthetic unknown-usage failure")
            return ModelResponse("done", usage=TokenUsage(23000, 1001))

    class Box:
        name = "fake-container"
        alive = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> Box:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            del exc_type, exc, traceback

        async def execute(
            self, argv: tuple[str, ...], *, cwd: str = "/", timeout_seconds: float = 30
        ) -> ProcessResult:
            del cwd, timeout_seconds
            return ProcessResult(argv, 0, "", "")

        def runner(self, *args: object) -> _Host:
            del args
            return _Host()

    async def capture(*args: object) -> str:
        del args
        return "candidate source"

    async def verify(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(public_lite, "DockerSandbox", Box)
    monkeypatch.setattr(public_lite, "capture_candidate", capture)
    monkeypatch.setattr(public_lite, "verify_candidate", verify)
    report = await public_lite.run_study(
        selection,
        sources=tmp_path / "sources",
        output=output,
        host=_Host(),
        model_factory=FakeModel,
    )
    aggregate = report["aggregate"]
    rows = report["results"]
    assert isinstance(aggregate, dict)
    assert isinstance(rows, list)
    assert len(rows) == 12
    if unknown_usage:
        assert calls == 1
        assert aggregate["unknown_usage_calls"] == 1
        assert sum(r["status"] == "not_run" for r in rows) == 11
    else:
        assert calls == 10
        assert aggregate["total_tokens"] == 240010
        assert aggregate["token_overrun"] == 10
        assert aggregate["success_rate"] == pytest.approx(10 / 12)
        assert sum(r["status"] == "not_run" for r in rows) == 2
    for task in selection.tasks[:calls]:
        fixture = output / "runs" / task.id / "agent_input"
        combined = "\n".join(p.read_text() for p in fixture.iterdir())
        assert "PRIVATE_REFERENCE" not in combined
        assert "PRIVATE_TEST" not in combined

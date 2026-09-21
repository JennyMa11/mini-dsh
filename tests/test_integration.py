"""Integration tests that cross an adapter or process boundary."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from minidsh.adapters import (
    JsonlTraceWriter,
    JsonSessionStore,
    ModelAdapterError,
    OpenAICompatibleModel,
    SessionStoreError,
)
from minidsh.domain import (
    Event,
    Message,
    ModelRequest,
    Role,
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from minidsh.infrastructure import (
    CommandPolicy,
    DockerCommandRunner,
    LocalCommandRunner,
    NetworkPolicy,
    NetworkRule,
    ProcessResult,
    WorkspaceSandbox,
)

_PYTHON = Path(sys.executable).name


def _sandbox(tmp_path: Path) -> WorkspaceSandbox:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WorkspaceSandbox(workspace)


# --- LocalCommandRunner -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_runner_executes_in_the_workspace(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.root / "data.txt").write_text("payload\n", encoding="utf-8")
    runner = LocalCommandRunner(sandbox, CommandPolicy(allowed_commands=frozenset({_PYTHON})))

    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import pathlib; print(pathlib.Path('data.txt').read_text().strip())",
        )
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "payload"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.argv[0] == sys.executable


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_runner_reports_non_zero_exit_without_raising(tmp_path: Path) -> None:
    runner = LocalCommandRunner(
        _sandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({_PYTHON}))
    )

    result = await runner.run((sys.executable, "-c", "raise SystemExit(3)"))

    assert result.exit_code == 3
    assert result.timed_out is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_runner_resolves_a_relative_subdirectory(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.root / "nested").mkdir()
    (sandbox.root / "nested" / "inner.txt").write_text("inner\n", encoding="utf-8")
    runner = LocalCommandRunner(sandbox, CommandPolicy(allowed_commands=frozenset({_PYTHON})))

    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import pathlib; print(pathlib.Path('inner.txt').read_text().strip())",
        ),
        cwd="nested",
    )

    assert result.stdout.strip() == "inner"


# --- DockerCommandRunner ------------------------------------------------------


class _RecordingRunner:
    """Capture the translated host argv instead of invoking Docker."""

    def __init__(self) -> None:
        self.argv: tuple[str, ...] = ()

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_runner_maps_workspace_paths_and_disables_network_by_default(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.root / "pkg").mkdir()
    host = _RecordingRunner()
    runner = DockerCommandRunner(
        sandbox,
        CommandPolicy(allowed_commands=frozenset({"python"})),
        host,
        image="python:3.12-slim",
    )

    result = await runner.run(("python", "-V"), cwd="pkg")

    assert result.argv == ("python", "-V")
    argv = host.argv
    assert argv[:2] == ("docker", "run")
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--workdir") + 1] == "/workspace/pkg"
    assert argv[argv.index("--volume") + 1] == f"{sandbox.root}:/workspace:rw"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[-2:] == ("python", "-V")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_runner_attaches_a_bridge_network_when_a_rule_exists(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    host = _RecordingRunner()
    network = NetworkPolicy(frozenset({NetworkRule("pypi.org", "tcp", frozenset({443}))}))
    runner = DockerCommandRunner(
        sandbox,
        CommandPolicy(allowed_commands=frozenset({"python"}), network=network),
        host,
        image="python:3.12-slim",
        network=network,
    )

    await runner.run(("python", "-V"))

    assert host.argv[host.argv.index("--network") + 1] == "bridge"
    assert host.argv[host.argv.index("--workdir") + 1] == "/workspace"


@pytest.mark.integration
def test_docker_runner_rejects_an_invalid_image_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="image"):
        DockerCommandRunner(
            _sandbox(tmp_path),
            CommandPolicy(allowed_commands=frozenset({"python"})),
            _RecordingRunner(),
            image="bad image",
        )


# --- JsonlTraceWriter ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trace_writer_appends_versioned_records(tmp_path: Path) -> None:
    trace = tmp_path / "traces" / "run.jsonl"
    writer = JsonlTraceWriter(trace)

    await writer.append(Event("run_started", "run-1", 0, {"task": "fix"}))
    await writer.append(Event("run_finished", "run-1", 2, {"status": "finished"}))

    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]

    assert [line["name"] for line in lines] == ["run_started", "run_finished"]
    assert all(line["schema_version"] == "1.0" for line in lines)
    assert lines[1]["step_id"] == 2
    assert lines[0]["occurred_at"].endswith("+00:00")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trace_writer_is_safe_under_concurrent_subscribers(tmp_path: Path) -> None:
    trace = tmp_path / "run.jsonl"
    writer = JsonlTraceWriter(trace)

    await writer.append(Event("step", "run-1", 1, {}))
    await writer.append(Event("step", "run-1", 2, {}))
    await writer.append(Event("step", "run-1", 3, {}))

    lines = trace.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["step_id"] for line in lines] == [1, 2, 3]


# --- JsonSessionStore ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_store_round_trips_a_run(tmp_path: Path) -> None:
    store = JsonSessionStore(_sandbox(tmp_path).root)
    result = RunResult(
        "run-42",
        RunStatus.FINISHED,
        "done",
        3,
        TokenUsage(10, 5, 0.0001),
        2,
        12.5,
        None,
        (
            Message(Role.USER, "fix it"),
            Message(
                Role.ASSISTANT,
                "",
                tool_calls=(ToolCall("c1", "read_file", {"path": "a.txt"}),),
            ),
            Message(Role.TOOL, "contents", name="read_file", tool_call_id="c1"),
        ),
    )

    await store.save(result)
    loaded = await store.load("run-42")

    assert loaded == result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_store_returns_none_for_an_unknown_run(tmp_path: Path) -> None:
    store = JsonSessionStore(_sandbox(tmp_path).root)

    assert await store.load("missing-run") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_store_rejects_unsafe_run_ids(tmp_path: Path) -> None:
    store = JsonSessionStore(_sandbox(tmp_path).root)

    for run_id in ("../escape", "a/b", ""):
        with pytest.raises(SessionStoreError, match="unsafe characters"):
            await store.load(run_id)


# --- OpenAICompatibleModel ----------------------------------------------------


class _ProviderHandler(BaseHTTPRequestHandler):
    """Deterministic stand-in for a Chat Completions endpoint."""

    response_body: ClassVar[dict[str, Any]] = {}
    status_code: ClassVar[int] = 200
    last_request: ClassVar[dict[str, Any]] = {}
    last_headers: ClassVar[dict[str, str]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_request = json.loads(self.rfile.read(length))
        type(self).last_headers = dict(self.headers.items())
        payload = json.dumps(type(self).response_body).encode()
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        del args


@pytest.fixture
def provider() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openai_compatible_model_parses_a_provider_response(provider: Any) -> None:
    _ProviderHandler.status_code = 200
    _ProviderHandler.response_body = {
        "choices": [
            {
                "message": {
                    "content": "done",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    model = OpenAICompatibleModel(
        endpoint=f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
        model="test-model",
        api_key="test-key",
    )

    response = await model.complete(ModelRequest((Message(Role.USER, "hi"),)))

    assert response.content == "done"
    assert response.tool_calls == (ToolCall("c1", "read_file", {"path": "a.txt"}),)
    assert response.finish_reason == "tool_calls"
    assert response.usage == TokenUsage(7, 3)
    assert _ProviderHandler.last_request["model"] == "test-model"
    assert _ProviderHandler.last_headers["Authorization"] == "Bearer test-key"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openai_compatible_model_sends_tool_schemas(provider: Any) -> None:
    _ProviderHandler.status_code = 200
    _ProviderHandler.response_body = {"choices": [{"message": {"content": "ok"}}]}
    model = OpenAICompatibleModel(
        endpoint=f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
        model="test-model",
        api_key="test-key",
    )

    await model.complete(
        ModelRequest(
            (Message(Role.USER, "hi"),),
            (ToolDefinition("read_file", "read", {"type": "object"}),),
        )
    )

    tools = _ProviderHandler.last_request["tools"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "read_file"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openai_compatible_model_raises_on_provider_failure(provider: Any) -> None:
    _ProviderHandler.status_code = 500
    _ProviderHandler.response_body = {"error": "boom"}
    model = OpenAICompatibleModel(
        endpoint=f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
        model="test-model",
        api_key="test-key",
    )

    with pytest.raises(ModelAdapterError, match="model request failed"):
        await model.complete(ModelRequest((Message(Role.USER, "hi"),)))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openai_compatible_model_rejects_invalid_tool_arguments(provider: Any) -> None:
    _ProviderHandler.status_code = 200
    _ProviderHandler.response_body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_file", "arguments": "{"}}
                    ],
                }
            }
        ]
    }
    model = OpenAICompatibleModel(
        endpoint=f"http://127.0.0.1:{provider.server_port}/v1/chat/completions",
        model="test-model",
        api_key="test-key",
    )

    with pytest.raises(ModelAdapterError, match="invalid tool arguments"):
        await model.complete(ModelRequest((Message(Role.USER, "hi"),)))

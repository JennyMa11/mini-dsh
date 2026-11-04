"""Offline contracts for the local browser presentation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.collaboration import build_multi_agent_runtime
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelRequest, ModelResponse, ToolCall, ToolObservation, ToolStatus
from minidsh.gui import GuiSession, handle_connection
from minidsh.tools import ToolRuntime


def _session(tmp_path: Path, model: ScriptedModel) -> GuiSession:
    options = RuntimeOptions()
    return GuiSession(
        workspace=tmp_path,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=model, options=options
        ),
        model_label="scripted",
        options=options,
        cost_reportable=False,
    )


@pytest.mark.asyncio
async def test_gui_follow_up_clear_and_resume(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content=s) for s in ("first", "second", "third")])
    session = _session(tmp_path, model)
    await session.action("run", {"task": "one"})
    await session.wait()
    saved = str(session.snapshot()["run_id"])
    await session.action("run", {"task": "two"})
    await session.wait()
    assert [m.content for m in model.requests[1].messages[1:]] == ["one", "first", "two"]
    await session.action("clear", {})
    assert session.snapshot()["messages"] == []
    await session.action("resume", {"run_id": saved})
    await session.action("run", {"task": "three"})
    await session.wait()
    assert [m.content for m in model.requests[2].messages[1:]] == ["one", "first", "three"]
    assert len(list((tmp_path / ".minidsh/traces").glob("*.jsonl"))) == 3
    assert session.snapshot()["metrics"]["cost_usd"] is None


@pytest.mark.asyncio
async def test_gui_default_permissions_and_redaction(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("w", "write_file", {"path": "bad.txt", "content": "secret=hidden"}),
                )
            ),
            ModelResponse(content="api_key=hidden"),
        ]
    )
    session = _session(tmp_path, model)
    await session.action("run", {"task": "change"})
    await session.wait()
    assert not (tmp_path / "bad.txt").exists()
    assert "hidden" not in str(session.snapshot())
    assert "invalid" in str(session.snapshot())
    with pytest.raises(ValueError, match="run_id"):
        await session.action("resume", {"run_id": "../../outside"})


@pytest.mark.asyncio
async def test_gui_busy_cancel_and_continue(tmp_path: Path) -> None:
    started = asyncio.Event()

    class BlockingModel(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if not started.is_set():
                started.set()
                await asyncio.Event().wait()
            return await super().complete(request)

    session = _session(tmp_path, BlockingModel([ModelResponse(content="recovered")]))
    await session.action("run", {"task": "wait"})
    await started.wait()
    for action, data in [
        ("run", {"task": "duplicate"}),
        ("clear", {}),
        ("diff", {}),
        ("settings", {}),
    ]:
        with pytest.raises(ValueError, match="running"):
            await session.action(action, data)
    await session.action("cancel", {})
    await session.wait()
    assert session.snapshot()["status"] == "cancelled"
    await session.action("run", {"task": "continue"})
    await session.wait()
    assert session.snapshot()["status"] == "finished"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_auth_origin_validation_and_run(tmp_path: Path) -> None:
    session = _session(tmp_path, ScriptedModel([ModelResponse(content="hello")]))

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, session=session, token="test-token")

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def request(path: str, headers: str = "", body: str | None = None) -> str:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        method = "GET" if body is None else "POST"
        payload = body or ""
        writer.write(
            (
                f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"{headers}Content-Length: {len(payload.encode())}\r\n\r\n{payload}"
            ).encode()
        )
        await writer.drain()
        response_header = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in response_header.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        response_body = await reader.readexactly(length)
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()
        return (response_header + response_body).decode()

    try:
        assert "200 OK" in await request("/")
        assert "400 Bad Request" in await request("/", "Host: evil.test\r\n")
        assert "403 Forbidden" in await request("/api/state")
        auth = "X-MiniDSH-Token: test-token\r\n"
        assert "403 Forbidden" in await request(
            "/api/state", auth + "Origin: https://evil.test\r\n"
        )
        assert "400 Bad Request" in await request("/api/run", auth, '{"task": 42}')
        assert "400 Bad Request" in await request("/api/run", auth, '{"task": ""}')
        assert "400 Bad Request" in await request(
            "/api/run", auth, '{"task": "ok", "allow_write": true}'
        )
        assert "400 Bad Request" in await request("/api/run", auth, "not JSON")
        assert "400 Bad Request" in await request("/api/run", auth, "x" * 66_000)
        assert "404 Not Found" in await request("/api/missing", auth)
        assert "200 OK" in await request("/api/run", auth, '{"task": "hi"}')
        await session.wait()
        assert '"finished"' in await request("/api/state", auth)
        assert "404 Not Found" in await request("/../pyproject.toml")
    finally:
        server.close()
        await server.wait_closed()
        await session.close()


@pytest.mark.asyncio
async def test_cancel_before_start_and_bounded_views(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="x" * 30_000)])
    session = _session(tmp_path, model)
    await session.action("run", {"task": "cancel before scheduling"})
    await session.action("cancel", {})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not session.snapshot()["busy"]
    assert session.snapshot()["status"] == "cancelled"
    assert not model.requests
    await session.action("run", {"task": "large response"})
    await session.wait()
    assert "展示已截断" in str(session.snapshot()["messages"])
    assert len(str(session.snapshot()["messages"])) < 21_000


@pytest.mark.asyncio
async def test_failed_start_can_retry_without_losing_controls(tmp_path: Path) -> None:
    options = RuntimeOptions()

    def unavailable() -> LocalRuntime:
        raise ValueError("api_key=hidden")

    session = GuiSession(
        workspace=tmp_path,
        runtime_factory=unavailable,
        model_label="missing",
        options=options,
        cost_reportable=False,
    )
    await session.action("run", {"task": "hello"})
    await session.wait()
    assert session.snapshot()["status"] == "failed"
    assert "hidden" not in str(session.snapshot())
    await session.action("clear", {})
    assert session.snapshot()["error"] is None


@pytest.mark.asyncio
async def test_diff_preserves_truncated_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(
        self: ToolRuntime,
        call: ToolCall,
        run_id: str,
        step_id: int,
    ) -> ToolObservation:
        del self, run_id, step_id
        assert call.name == "git"
        assert call.arguments == {"action": "diff", "paths": []}
        return ToolObservation(
            call.call_id,
            "git",
            ToolStatus.SUCCEEDED,
            {"preview": "diff --git a/code.py b/code.py\n+api_key=hidden"},
            truncated=True,
            artifacts=(".minidsh/artifacts/diff.json",),
        )

    monkeypatch.setattr(ToolRuntime, "execute", execute)
    session = _session(tmp_path, ScriptedModel([]))
    await session.action("diff", {})
    await session.wait()
    diff = str(session.snapshot()["diff"])
    assert "diff --git" in diff
    assert "hidden" not in diff
    assert ".minidsh/artifacts/diff.json" in diff
    assert "没有" not in diff


@pytest.mark.asyncio
async def test_diff_recovers_stdout_from_truncated_json_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(
        self: ToolRuntime,
        call: ToolCall,
        run_id: str,
        step_id: int,
    ) -> ToolObservation:
        del self, run_id, step_id
        return ToolObservation(
            call.call_id,
            "git",
            ToolStatus.SUCCEEDED,
            {
                "preview": '{"exit_code": 0, "stderr": "", "stdout": "diff --git a.py\\n+new\\n',
                "original_characters": 90_000,
            },
            truncated=True,
            artifacts=(".minidsh/artifacts/diff.json",),
        )

    monkeypatch.setattr(ToolRuntime, "execute", execute)
    session = _session(tmp_path, ScriptedModel([]))
    await session.action("diff", {})
    await session.wait()
    diff = str(session.snapshot()["diff"])
    assert "diff --git" in diff
    assert "+new" in diff
    assert '"stdout"' not in diff
    assert ".minidsh/artifacts/diff.json" in diff


@pytest.mark.asyncio
async def test_gui_settings_enable_collaboration_and_show_child_results(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse("single answer"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "delegate_sub_agents",
                        {
                            "tasks": [{"name": "reviewer", "role": "review", "task": "inspect"}],
                        },
                    ),
                )
            ),
            ModelResponse("combined answer"),
        ]
    )
    options = RuntimeOptions(enabled_tools=frozenset({"read_file"}))

    def factory(selected: RuntimeOptions, api_key: str | None = None) -> LocalRuntime:
        del api_key
        if selected.multi_agent_enabled:
            return build_multi_agent_runtime(
                workspace=tmp_path,
                model=model,
                options=selected,
                subagent_model_factory=lambda: ScriptedModel([ModelResponse("child finding")]),
            )
        return build_local_runtime(workspace=tmp_path, model=model, options=selected)

    session = GuiSession(
        workspace=tmp_path,
        runtime_factory=lambda: factory(options),
        settings_factory=factory,
        model_label="scripted",
        options=options,
        cost_reportable=False,
    )
    await session.action("run", {"task": "first"})
    await session.wait()
    await session.action(
        "settings",
        {"multi_agent_enabled": True, "subagent_max_agents": 3, "subagent_max_parallel": 2},
    )
    await session.action("run", {"task": "review with a Sub Agent"})
    await session.wait()
    state = session.snapshot()
    assert state["multi_agent_enabled"] is True
    assert state["collaboration"]["editable"] is True
    assert state["collaboration"]["max_agents"] == 3
    assert state["sub_agents"][0]["name"] == "reviewer"
    assert state["sub_agents"][0]["status"] == "finished"
    assert state["sub_agents"][0]["answer"] == "child finding"
    assert state["permissions"]["write"] is False
    assert [message["content"] for message in state["messages"]][:2] == ["first", "single answer"]
    await session.action(
        "settings",
        {"multi_agent_enabled": False, "subagent_max_agents": 3, "subagent_max_parallel": 2},
    )
    assert session.snapshot()["multi_agent_enabled"] is False


@pytest.mark.asyncio
async def test_gui_settings_failure_keeps_previous_configuration(tmp_path: Path) -> None:
    options = RuntimeOptions()

    def unavailable(selected: RuntimeOptions, api_key: str | None) -> LocalRuntime:
        del selected, api_key
        raise ValueError("adapter unavailable")

    session = GuiSession(
        workspace=tmp_path,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=ScriptedModel([]), options=options
        ),
        settings_factory=unavailable,
        model_label="scripted",
        options=options,
        cost_reportable=False,
    )
    with pytest.raises(ValueError, match="unavailable"):
        await session.action(
            "settings",
            {"multi_agent_enabled": True, "subagent_max_agents": 4, "subagent_max_parallel": 2},
        )
    assert session.snapshot()["multi_agent_enabled"] is False


@pytest.mark.asyncio
async def test_gui_settings_cannot_change_permissions(tmp_path: Path) -> None:
    session = _session(tmp_path, ScriptedModel([]))
    with pytest.raises(ValueError, match="allow_write"):
        await session.action(
            "settings",
            {
                "multi_agent_enabled": True,
                "subagent_max_agents": 4,
                "subagent_max_parallel": 2,
                "allow_write": True,
            },
        )
    assert session.snapshot()["permissions"]["write"] is False


@pytest.mark.asyncio
async def test_gui_api_key_is_memory_only_and_reused_without_echo(tmp_path: Path) -> None:
    received: list[str | None] = []
    options = RuntimeOptions()

    def factory(selected: RuntimeOptions, api_key: str | None) -> LocalRuntime:
        received.append(api_key)
        return build_local_runtime(
            workspace=tmp_path, model=ScriptedModel([ModelResponse("hello")]), options=selected
        )

    session = GuiSession(
        workspace=tmp_path,
        runtime_factory=lambda: factory(options, None),
        settings_factory=factory,
        credentials_editable=True,
        model_label="test provider",
        options=options,
        cost_reportable=False,
    )
    key = "synthetic_gui_key_only"
    await session.action(
        "settings",
        {
            "multi_agent_enabled": False,
            "subagent_max_agents": 4,
            "subagent_max_parallel": 2,
            "api_key": key,
        },
    )
    assert received == [key]
    assert session.snapshot()["credentials"]["configured"] is True
    assert key not in str(session.snapshot())
    await session.action(
        "settings",
        {"multi_agent_enabled": False, "subagent_max_agents": 3, "subagent_max_parallel": 1},
    )
    assert received == [key, key]
    await session.action("run", {"task": "hello"})
    await session.wait()
    for path in (tmp_path / ".minidsh").rglob("*.json*"):
        assert key not in path.read_text(encoding="utf-8")
    await session.close()
    assert session.snapshot()["credentials"]["configured"] is False

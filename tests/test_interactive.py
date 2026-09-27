"""Interactive CLI contracts, using offline model responses and temporary workspaces."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.cli import main
from minidsh.domain import ModelRequest, ModelResponse, Role, ToolCall


def _input(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    pending = iter(lines)

    def read(prompt: str = "") -> str:
        del prompt
        try:
            return next(pending)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", read)


def _model(monkeypatch: pytest.MonkeyPatch, responses: list[ModelResponse]) -> ScriptedModel:
    model = ScriptedModel(responses)
    monkeypatch.setattr(ScriptedModel, "from_file", lambda _path: model)
    return model


def _chat(tmp_path: Path, *options: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["chat", "--workspace", str(tmp_path), "--script", "offline.json", *options])
    assert exc.value.code == 0


def test_no_arguments_opens_chat_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _input(monkeypatch, ["/help", "/status", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "DeepSeek" in output
    assert "/clear" in output
    assert "read-only" in output


def test_chat_keeps_follow_ups_and_clear_starts_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _model(monkeypatch, [ModelResponse(content=s) for s in ("first", "second", "new")])
    _input(monkeypatch, ["one", "two", "/clear", "three", "/exit"])
    _chat(tmp_path)
    assert [m.content for m in model.requests[1].messages[1:]] == ["one", "first", "two"]
    assert [m.content for m in model.requests[2].messages[1:]] == ["three"]
    traces = list((tmp_path / ".minidsh/traces").glob("*.jsonl"))
    assert len(traces) == 3
    assert len(list((tmp_path / ".minidsh/sessions").glob("*.json"))) == 3
    assert "second" in capsys.readouterr().out


def test_chat_displays_progress_and_does_not_grant_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "code.py"
    target.write_text("original")
    _model(
        monkeypatch,
        [
            ModelResponse(
                tool_calls=(ToolCall("a", "write_file", {"path": "code.py", "content": "changed"}),)
            ),
            ModelResponse(content="could not write"),
        ],
    )
    _input(monkeypatch, ["edit", "/exit"])
    _chat(tmp_path)
    assert target.read_text() == "original"
    output = capsys.readouterr().out
    assert "write_file" in output
    assert "invalid" in output


def test_chat_resume_restores_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _model(monkeypatch, [ModelResponse(content="remember this")])
    _input(monkeypatch, ["one", "/exit"])
    _chat(tmp_path)
    run_id = next((tmp_path / ".minidsh/sessions").glob("*.json")).stem
    model = _model(monkeypatch, [ModelResponse(content="remembered")])
    _input(monkeypatch, ["two", "/exit"])
    _chat(tmp_path, "--resume", run_id)
    assert [m.content for m in model.requests[0].messages[1:]] == ["one", "remember this", "two"]


def test_cancelled_turn_returns_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class CancelOnce(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if not self.requests:
                self.requests.append(request)
                raise asyncio.CancelledError
            return await super().complete(request)

    model = CancelOnce([ModelResponse(content="recovered")])
    monkeypatch.setattr(ScriptedModel, "from_file", lambda _path: model)
    _input(monkeypatch, ["cancel me", "continue", "/exit"])
    _chat(tmp_path)
    records = [json.loads(p.read_text()) for p in (tmp_path / ".minidsh/sessions").glob("*.json")]
    assert {r["status"] for r in records} == {"cancelled", "finished"}
    assert any(
        m.role is Role.USER and m.content == "cancel me" for m in model.requests[-1].messages
    )


def test_missing_credentials_returns_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MINIDSH_API_KEY_ENV", raising=False)
    monkeypatch.delenv("MINIDSH_MODEL_PROFILE", raising=False)
    monkeypatch.delenv("MINIDSH_MODEL_ENDPOINT", raising=False)
    _input(monkeypatch, ["hello", "/help", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY" in out
    assert "/clear" in out
    assert not (tmp_path / ".minidsh/traces").exists()


def test_multiline_and_invalid_resume_keep_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _model(monkeypatch, [ModelResponse(content="ok"), ModelResponse(content="still here")])
    _input(monkeypatch, ["first\\", "second", "/resume ../../outside", "continue"])
    _chat(tmp_path)
    assert model.requests[0].messages[-1].content == "first\nsecond"
    assert model.requests[1].messages[1].content == "first\nsecond"
    assert "Cannot resume" in capsys.readouterr().out


def test_turns_share_one_event_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loops: list[asyncio.AbstractEventLoop] = []

    class LoopAwareModel(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            loops.append(asyncio.get_running_loop())
            return await super().complete(request)

    model = LoopAwareModel([ModelResponse(content="first"), ModelResponse(content="second")])
    monkeypatch.setattr(ScriptedModel, "from_file", lambda _path: model)
    _input(monkeypatch, ["one", "two", "/exit"])
    _chat(tmp_path)
    assert len(loops) == 2
    assert loops[0] is loops[1]


@pytest.mark.e2e
def test_interactive_harness_reads_patches_and_runs_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_sum(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    script = tmp_path / "responses.json"
    script.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {"call_id": "r", "name": "read_file", "arguments": {"path": "calc.py"}}
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "call_id": "p",
                            "name": "patch_file",
                            "arguments": {
                                "path": "calc.py",
                                "expected": "a - b",
                                "replacement": "a + b",
                            },
                        }
                    ]
                },
                {"tool_calls": [{"call_id": "t", "name": "test", "arguments": {}}]},
                {"content": "Fixed and tested."},
                {"content": "The earlier change replaced subtraction with addition."},
            ]
        )
    )
    _input(monkeypatch, ["fix it", "what changed?", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "chat",
                "--workspace",
                str(tmp_path),
                "--script",
                str(script),
                "--allow-write",
                "--allow-shell",
                "--allow-command",
                "python",
                "--test-command",
                "python -m unittest discover -s .",
            ]
        )
    assert exc.value.code == 0
    assert "a + b" in (tmp_path / "calc.py").read_text()
    out = capsys.readouterr().out
    assert "test: succeeded" in out
    traces = [
        json.loads(line)
        for p in (tmp_path / ".minidsh/traces").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    test_events = [
        e for e in traces if e["name"] == "tool_call_completed" and e["data"]["tool"] == "test"
    ]
    assert test_events[0]["data"]["result"]["exit_code"] == 0

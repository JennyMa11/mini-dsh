"""Interactive CLI contracts, using offline model responses and temporary workspaces."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from minidsh.adapters import ModelProfile, ScriptedModel
from minidsh.cli import main
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelRequest, ModelResponse, Role, ToolCall
from minidsh.interactive import InteractiveChat


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
        main(
            [
                "chat",
                "--workspace",
                str(tmp_path),
                "--script",
                "offline.json",
                "--approval-mode",
                "configured",
                *options,
            ]
        )
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
    assert "Permissions: write" in output


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
    assert "Thinking" not in output
    events = [
        json.loads(line)
        for path in (tmp_path / ".minidsh/traces").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert any(event["name"] == "model_call_started" for event in events)


def test_tool_progress_reports_the_observed_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "code.py").write_text("value = 1")
    _model(
        monkeypatch,
        [
            ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "code.py"}),)),
            ModelResponse(content="read it"),
        ],
    )
    _input(monkeypatch, ["inspect", "/exit"])
    _chat(tmp_path)
    assert re.search(r"← read_file: succeeded · \d+ms", capsys.readouterr().out)


def test_status_bar_line_reports_permissions_and_last_turn(tmp_path: Path) -> None:
    options = RuntimeOptions(allow_write=True, allow_shell=True)
    chat = InteractiveChat(
        workspace=tmp_path,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=ScriptedModel([]), options=options
        ),
        model_label="deepseek",
        options=options,
        cost_reportable=False,
    )
    line = chat._status_line()
    assert "deepseek" in line
    assert tmp_path.name in line
    assert "write+shell" in line
    assert "no turns yet" in line


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
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: (_ for _ in ()).throw(EOFError()))
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
                "work",
                "--workspace",
                str(tmp_path),
                "--script",
                str(script),
                "--test-command",
                "python -m unittest discover -s .",
                "--approval-mode",
                "configured",
            ]
        )
    assert exc.value.code == 0
    assert "a + b" in (tmp_path / "calc.py").read_text()
    out = capsys.readouterr().out
    assert "test: passed" in out
    traces = [
        json.loads(line)
        for p in (tmp_path / ".minidsh/traces").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    test_events = [
        e for e in traces if e["name"] == "tool_call_completed" and e["data"]["tool"] == "test"
    ]
    assert test_events[0]["data"]["result"]["exit_code"] == 0
    assert "Verification: passed" in out
    assert "Changed: calc.py" in out


def test_work_loads_project_instructions_and_keeps_latest_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "AGENTS.md").write_text("Keep public function signatures unchanged.")
    model = _model(monkeypatch, [ModelResponse(content="one"), ModelResponse(content="two")])
    _input(monkeypatch, ["/tools", "inspect", "now explain", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path), "--script", "offline.json"])
    assert exc.value.code == 0
    request = model.requests[-1]
    assert any("Keep public function signatures unchanged." in m.content for m in request.messages)
    assert any(m.content == "now explain" for m in request.messages)
    names = {t.name for t in request.tools}
    assert {"list_files", "patch_file", "read_file", "shell"} <= names
    assert "test" not in names
    assert "Execution backend: local" in request.messages[0].content
    assert "same host machine" in request.messages[0].content
    output = capsys.readouterr().out
    assert "Registered tools:" in output
    assert "patch_file" in output
    assert "shell" in output.split("Registered tools:", 1)[1].splitlines()[0]


@pytest.mark.integration
def test_work_default_shell_runs_an_allowlisted_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = _model(
        monkeypatch,
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("command", "shell", {"argv": ["python3", "-c", "print(888)"]}),
                )
            ),
            ModelResponse(content="command completed"),
        ],
    )
    _input(monkeypatch, ["run the command", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "work",
                "--workspace",
                str(tmp_path),
                "--script",
                "offline.json",
                "--approval-mode",
                "configured",
            ]
        )
    assert exc.value.code == 0
    assert "shell" in {t.name for t in model.requests[0].tools}
    assert any(m.role is Role.TOOL and "888" in m.content for m in model.requests[-1].messages)
    assert "shell: succeeded" in capsys.readouterr().out


def test_work_can_disable_the_default_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _model(monkeypatch, [ModelResponse(content="ok")])
    _input(monkeypatch, ["/tools", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "work",
                "--workspace",
                str(tmp_path),
                "--script",
                "offline.json",
                "--no-allow-shell",
            ]
        )
    assert exc.value.code == 0
    tools_line = capsys.readouterr().out.split("Registered tools:", 1)[1].splitlines()[0]
    assert "shell" not in tools_line


@pytest.mark.integration
def test_work_explicit_shell_grant_executes_a_model_selected_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model = _model(
        monkeypatch,
        [
            ModelResponse(
                tool_calls=(ToolCall("command", "shell", {"argv": ["python", "-c", "print(777)"]}),)
            ),
            ModelResponse(content="command completed"),
        ],
    )
    _input(monkeypatch, ["/tools", "run the command", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "work",
                "--workspace",
                str(tmp_path),
                "--script",
                "offline.json",
                "--allow-shell",
                "--allow-command",
                "python",
                "--approval-mode",
                "configured",
            ]
        )
    assert exc.value.code == 0
    assert "shell" in {t.name for t in model.requests[0].tools}
    assert any(m.role is Role.TOOL and "777" in m.content for m in model.requests[-1].messages)
    output = capsys.readouterr().out
    assert "shell" in output.split("Registered tools:", 1)[1].splitlines()[0]
    assert "shell: succeeded" in output


def test_work_outline_request_preserves_scope_and_treats_missing_instructions_as_optional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = "我现在想做一个个人的简单网页，可以先给出我一个大纲吗"  # noqa: RUF001 - actual Chinese input
    model = _model(monkeypatch, [ModelResponse(content="outline")])
    _input(monkeypatch, [task, "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path), "--script", "offline.json"])
    assert exc.value.code == 0
    request = model.requests[0]
    assert any(m.role is Role.USER and m.content == task for m in request.messages)
    prompt = request.messages[0].content
    assert "latest user request" in prompt
    assert "outline" in prompt
    assert "do not edit files" in prompt
    assert "personal website" in prompt
    assert "terminal coding assistant" in prompt
    assert "1-3 short sentences" in prompt
    assert "Do not repeat tool logs" in prompt
    assert "When the user requests detail" in prompt
    assert not any(m.name == "plan_state" for m in request.messages)
    instructions = next(m for m in request.messages if m.name == "workspace_instructions")
    assert "optional" in instructions.content
    output = capsys.readouterr().out
    assert f"Request: {task}" in output
    assert "optional AGENTS.md not found" in output
    assert "read_file: failed" not in output
    assert not list(tmp_path.glob("*.html"))


def test_optional_instruction_status_does_not_hide_an_explicit_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _model(
        monkeypatch,
        [
            ModelResponse(tool_calls=(ToolCall("explicit", "read_file", {"path": "AGENTS.md"}),)),
            ModelResponse(content="file absent"),
        ],
    )
    _input(monkeypatch, ["read AGENTS.md", "/exit"])
    _chat(tmp_path)
    assert "read_file: failed" in capsys.readouterr().out


def test_work_can_explicitly_enable_the_editing_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = _model(monkeypatch, [ModelResponse(content="ok")])
    _input(monkeypatch, ["inspect code", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path), "--script", "offline.json", "--local-first"])
    assert exc.value.code == 0
    assert any(m.name == "plan_state" for m in model.requests[0].messages)


def test_work_respects_explicit_readonly_and_does_not_follow_instruction_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_INSTRUCTIONS")
    (workspace / "AGENTS.md").symlink_to(outside)
    model = _model(monkeypatch, [ModelResponse(content="ok")])
    _input(monkeypatch, ["inspect", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            ["work", "--workspace", str(workspace), "--script", "offline.json", "--no-allow-write"]
        )
    assert exc.value.code == 0
    request = model.requests[0]
    assert "OUTSIDE_INSTRUCTIONS" not in "\n".join(m.content for m in request.messages)
    assert "patch_file" not in {t.name for t in request.tools}
    output = capsys.readouterr().out
    assert "AGENTS.md" in output
    assert "read_file: failed" in output


@pytest.mark.integration
def test_terminal_reports_failed_test_and_invalidates_verification_after_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "calc.py").write_text("before")
    _model(
        monkeypatch,
        [
            ModelResponse(tool_calls=(ToolCall("t", "test", {}),)),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "p",
                        "patch_file",
                        {
                            "path": "calc.py",
                            "expected": "before",
                            "replacement": "after",
                        },
                    ),
                )
            ),
            ModelResponse(content="All tests passed."),
        ],
    )
    _input(monkeypatch, ["edit", "/exit"])
    _chat(
        tmp_path,
        "--allow-write",
        "--allow-shell",
        "--allow-command",
        "python",
        "--test-command",
        "python -c 'raise SystemExit(1)'",
    )
    output = capsys.readouterr().out
    assert "test: failed (exit 1)" in output
    assert "Verification: not run after the latest edit" in output


@pytest.mark.integration
def test_manual_test_feedback_is_available_to_the_followup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = _model(monkeypatch, [ModelResponse(content="explanation")])
    _input(monkeypatch, ["/test", "explain the result", "/exit"])
    _chat(
        tmp_path,
        "--allow-shell",
        "--allow-command",
        "python",
        "--test-command",
        "python -c 'print(123)'",
    )
    assert any(
        m.name == "verification_status" and "123" in m.content for m in model.requests[0].messages
    )


def test_key_entry_stays_in_memory_and_never_appears_in_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-private-terminal-key-1234567890"
    received: list[str] = []
    options = RuntimeOptions()
    model = ScriptedModel([ModelResponse(content="ok")])

    def factory() -> LocalRuntime:
        raise ValueError("Missing credential")

    def credentials(key: str) -> LocalRuntime:
        received.append(key)
        return build_local_runtime(workspace=tmp_path, model=model, options=options)

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: secret)
    _input(monkeypatch, ["/key", "hello", "/exit"])
    chat = InteractiveChat(
        workspace=tmp_path,
        runtime_factory=factory,
        model_label="test",
        options=options,
        cost_reportable=False,
        credential_factory=credentials,
    )
    assert chat.run() == 0
    assert received == [secret]
    assert secret not in capsys.readouterr().out
    for path in (tmp_path / ".minidsh").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()


@pytest.mark.integration
def test_manual_test_needs_no_provider_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _input(monkeypatch, ["/test", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path), "--test-command", "python -c 'print(321)'"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "test: passed" in output
    assert "321" in output
    assert "Thinking" not in output


def test_key_entry_refuses_echo_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    received: list[str] = []

    def credentials(key: str) -> LocalRuntime:
        received.append(key)
        return build_local_runtime(
            workspace=tmp_path, model=ScriptedModel([]), options=RuntimeOptions()
        )

    def insecure_prompt(_prompt: str) -> str:
        warnings.warn("Cannot control echo on the terminal", getpass.GetPassWarning, stacklevel=2)
        return "private-key"

    monkeypatch.setattr(getpass, "getpass", insecure_prompt)
    _input(monkeypatch, ["/key", "/exit"])
    chat = InteractiveChat(
        workspace=tmp_path,
        runtime_factory=lambda: credentials("unused"),
        model_label="test",
        options=RuntimeOptions(),
        cost_reportable=False,
        credential_factory=credentials,
    )
    assert chat.run() == 0
    assert received == []
    assert "cannot hide input" in capsys.readouterr().out


def test_first_task_configures_and_saves_key_then_next_start_reuses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    received: list[str] = []
    prompts: list[str] = []
    secret = "synthetic-saved-terminal-key"

    def provider(
        profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
    ) -> ScriptedModel:
        del temperature, max_attempts
        received.append(profile.api_key(environ))
        return ScriptedModel([ModelResponse(content=f"completed {secret}")])

    def key_input(prompt: str) -> str:
        prompts.append(prompt)
        return secret

    monkeypatch.setattr("minidsh.cli.build_provider_model", provider)
    monkeypatch.setattr(getpass, "getpass", key_input)
    for _ in range(2):
        _input(monkeypatch, ["hello", "/exit"])
        with pytest.raises(SystemExit) as exc:
            main(["work", "--workspace", str(tmp_path)])
        assert exc.value.code == 0
    assert received == [secret, secret]
    assert len(prompts) == 1
    credential_file = tmp_path / ".minidsh" / "credentials.toml"
    assert secret in credential_file.read_text()
    output = capsys.readouterr().out
    assert "Key saved" in output
    assert "workspace config" in output
    assert secret not in output
    assert "DEEPSEEK_API_KEY" not in os.environ
    for path in (tmp_path / ".minidsh").rglob("*"):
        if path.is_file() and path != credential_file:
            assert secret not in path.read_text()


@pytest.mark.parametrize("source", ["global config", "workspace config", "environment"])
def test_chat_uses_layered_credential_without_prompting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    global_file = tmp_path / "global" / "credentials.toml"
    global_file.parent.mkdir()
    global_file.write_text('[credentials]\nDEEPSEEK_API_KEY = "global-key"\n')
    expected = "global-key"
    if source != "global config":
        project_file = tmp_path / ".minidsh" / "credentials.toml"
        project_file.parent.mkdir()
        project_file.write_text('[credentials]\nDEEPSEEK_API_KEY = "workspace-key"\n')
        expected = "workspace-key"
    if source == "environment":
        monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key")
        expected = "environment-key"
    received: list[str] = []

    def provider(
        profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
    ) -> ScriptedModel:
        del temperature, max_attempts
        received.append(profile.api_key(environ))
        return ScriptedModel([ModelResponse(content="done")])

    def unexpected_prompt(_prompt: str) -> str:
        pytest.fail("An existing credential must not prompt again")

    monkeypatch.setattr("minidsh.cli.build_provider_model", provider)
    monkeypatch.setattr(getpass, "getpass", unexpected_prompt)
    _input(monkeypatch, ["hello", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path)])
    assert exc.value.code == 0
    assert received == [expected]


def test_key_save_failure_keeps_session_usable_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = RuntimeOptions()
    model = ScriptedModel([ModelResponse(content="done")])

    async def cannot_save(_key: str) -> Path:
        raise OSError("synthetic-private-key")

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "synthetic-private-key")
    _input(monkeypatch, ["hello", "/exit"])
    chat = InteractiveChat(
        workspace=tmp_path,
        runtime_factory=lambda: build_local_runtime(
            workspace=tmp_path, model=model, options=options
        ),
        model_label="test",
        options=options,
        cost_reportable=False,
        credential_factory=lambda _key: build_local_runtime(
            workspace=tmp_path, model=model, options=options
        ),
        credential_required="TEST_API_KEY",
        credential_saver=cannot_save,
    )
    assert chat.run() == 0
    assert model.requests
    output = capsys.readouterr().out
    assert "could not be saved" in output
    assert "synthetic-private-key" not in output


def test_workspace_preparation_spends_tool_budget_before_model_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "AGENTS.md").write_text("project instructions")
    model = _model(monkeypatch, [ModelResponse(content="must not run")])
    _input(monkeypatch, ["inspect", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "work",
                "--workspace",
                str(tmp_path),
                "--script",
                "offline.json",
                "--max-tool-calls",
                "1",
            ]
        )
    assert exc.value.code == 0
    assert model.requests == []
    assert "budget_exhausted" in capsys.readouterr().out
    events = [
        json.loads(line)
        for path in (tmp_path / ".minidsh/traces").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    calls = [e for e in events if e["name"] == "tool_call_completed"]
    assert len(calls) == 1
    assert calls[0]["data"]["tool"] == "read_file"


def test_truncated_project_instructions_stop_before_model_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "AGENTS.md").write_text("RULE " * 6000)
    model = _model(monkeypatch, [ModelResponse(content="must not run")])
    _input(monkeypatch, ["inspect", "/exit"])
    with pytest.raises(SystemExit) as exc:
        main(["work", "--workspace", str(tmp_path), "--script", "offline.json"])
    assert exc.value.code == 0
    assert model.requests == []
    assert "AGENTS.md exceeds the instruction limit" in capsys.readouterr().out

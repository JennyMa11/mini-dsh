"""Smoke tests for the command-line interface."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from minidsh.adapters import ModelProfile, ScriptedModel
from minidsh.cli import main
from minidsh.domain import ModelRequest, ModelResponse, ToolCall
from minidsh.gui import GuiSession


@pytest.mark.parametrize("command", ["run", "benchmark", "ablation"])
@pytest.mark.integration
def test_explicit_scripted_provider_overrides_environment_model_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MINIDSH_MODEL_PROFILE", "deepseek")

    def forbid_provider(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("explicit scripted mode must never construct a real provider")

    monkeypatch.setattr("minidsh.cli.build_provider_model", forbid_provider)
    script = tmp_path / "responses.json"
    script.write_text('[{"content": "offline answer"}]', encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "offline",
                "schema_version": "1.0",
                "tasks": [
                    {
                        "id": "task",
                        "goal": "answer",
                        "workspace": ".",
                        "response_script": "responses.json",
                        "success_argv": ["python", "-c", "pass"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    argv = [command]
    if command == "run":
        argv += ["answer", "--workspace", str(tmp_path), "--script", str(script)]
    else:
        argv += [
            str(manifest),
            "--output",
            str(tmp_path / "report.json"),
            "--verify-command",
            "python",
        ]
        if command == "ablation":
            argv += ["--suite", "tool_output"]
    argv += [
        "--provider",
        "scripted",
        "--input-usd-per-million",
        "3",
        "--output-usd-per-million",
        "15",
    ]
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 0
    if command == "run":
        assert "offline answer" in capsys.readouterr().out


def test_scripted_provider_rejects_conflicting_explicit_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "task",
                "--workspace",
                str(tmp_path),
                "--provider",
                "scripted",
                "--model-profile",
                "deepseek",
            ]
        )
    assert exit_info.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_doctor_reports_healthy_environment(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["doctor"])

    assert exit_info.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["python_ok"] is True


def test_version_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("mini-dsh ")


def _write_trace(path: Path, *, tool: str, context_tokens: int) -> None:
    records = [
        {
            "name": "context_built",
            "run_id": "r1",
            "step_id": 1,
            "data": {"strategy": "full_history", "estimated_tokens": context_tokens},
        },
        {
            "name": "model_call_completed",
            "run_id": "r1",
            "step_id": 1,
            "data": {
                "finish_reason": "tool_calls",
                "content": "working",
                "input_tokens": context_tokens,
                "output_tokens": 4,
                "duration_ms": 12.0,
            },
        },
        {
            "name": "tool_call_completed",
            "run_id": "r1",
            "step_id": 1,
            "data": {
                "tool": tool,
                "status": "succeeded",
                "duration_ms": 3.0,
                "attempt": 1,
                "result": {"ok": True},
            },
        },
        {
            "name": "run_finished",
            "run_id": "r1",
            "step_id": 1,
            "data": {"status": "finished", "duration_ms": 40.0},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_trace_replay_renders_every_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = tmp_path / "run.jsonl"
    _write_trace(trace, tool="read_file", context_tokens=100)

    with pytest.raises(SystemExit) as exit_info:
        main(["trace", "replay", str(trace)])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "**Context**" in out
    assert "`read_file`" in out
    assert "**State**" in out


def test_trace_replay_supports_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = tmp_path / "run.jsonl"
    _write_trace(trace, tool="search", context_tokens=50)

    with pytest.raises(SystemExit):
        main(["trace", "replay", str(trace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "finished"
    assert payload["steps"] == 1


def test_trace_diff_reports_the_token_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_trace(left, tool="read_file", context_tokens=100)
    _write_trace(right, tool="read_file", context_tokens=300)

    with pytest.raises(SystemExit) as exit_info:
        main(["trace", "diff", str(left), str(right), "--left-label", "base"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "`base` vs `right`" in out
    assert "+200" in out


def test_report_renders_a_saved_benchmark_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "name": "suite",
                "aggregate": {"tasks": 1, "success_rate": 1.0},
                "failure_layers": {"success": 1, "harness": 0, "agent": 0, "environment": 0},
                "failure_categories": {"none": 1},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["report", str(report)])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "# Benchmark report: suite" in out
    assert "## Outcome by layer" in out


def test_unknown_model_profile_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "do something",
                "--workspace",
                str(tmp_path),
                "--model-profile",
                "does-not-exist",
            ]
        )

    assert exit_info.value.code == 2
    assert "unknown model profile" in capsys.readouterr().err


def test_a_paid_profile_without_a_rate_card_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "do something",
                "--workspace",
                str(tmp_path),
                "--model-profile",
                "deepseek",
            ]
        )

    assert exit_info.value.code == 2
    assert "no declared rate card" in capsys.readouterr().err


def test_api_key_env_names_the_credential_for_an_ad_hoc_endpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MINIDSH_API_KEY_ENV`` must mean the same thing on every provider path.

    It is honoured for named profiles, so ignoring it for an explicit endpoint
    would make "where does my key come from" depend on which flag was used.
    The observable proof is that the error names the variable it looked for.
    """
    monkeypatch.setenv("MINIDSH_API_KEY_ENV", "MY_PROVIDER_TOKEN")
    monkeypatch.delenv("MY_PROVIDER_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "do something",
                "--workspace",
                str(tmp_path),
                "--provider",
                "openai-compatible",
                "--endpoint",
                "https://provider.example/v1/chat/completions",
                "--model",
                "example-model",
                "--input-usd-per-million",
                "1.0",
                "--output-usd-per-million",
                "1.0",
            ]
        )

    assert exit_info.value.code == 2
    assert "MY_PROVIDER_TOKEN" in capsys.readouterr().err


def test_config_profile_and_cli_can_revoke_file_permissions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    (tmp_path / "minidsh.toml").write_text(
        "[minidsh]\nallow_write = true\nallow_shell = true\n"
        '[profiles.review]\ncontext_strategy = "summary"\nrecent_messages = 3\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "config",
                "--workspace",
                str(tmp_path),
                "--profile",
                "review",
                "--no-allow-write",
                "--no-allow-shell",
                "--no-artifact-offload",
                "--enabled-tools",
                "read_file",
                "search",
            ]
        )
    assert exit_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["profile"] == "review"
    assert output["available_profiles"] == ["review"]
    assert output["options"]["context_strategy"] == "summary"
    assert output["options"]["recent_messages"] == 3
    assert output["options"]["allow_write"] is False
    assert output["options"]["allow_shell"] is False
    assert output["options"]["artifact_offload"] is False
    assert output["options"]["enabled_tools"] == ["read_file", "search"]
    assert output["sources"]["allow_write"] == "cli"


@pytest.mark.e2e
def test_cli_runs_an_offline_team_and_can_replay_a_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    parent = tmp_path / "parent.json"
    child = tmp_path / "child.json"
    trace = tmp_path / "team.jsonl"
    parent.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "delegate",
                            "name": "delegate_sub_agents",
                            "arguments": {
                                "tasks": [
                                    {"name": "reviewer", "task": "inspect", "role": "reviewer"}
                                ],
                            },
                        }
                    ]
                },
                {"content": "combined answer"},
            ]
        ),
        encoding="utf-8",
    )
    child.write_text('[{"content":"child answer"}]', encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "inspect with a Sub Agent",
                "--workspace",
                str(tmp_path),
                "--script",
                str(parent),
                "--subagent-script",
                str(child),
                "--multi-agent",
                "--trace",
                str(trace),
            ]
        )
    assert exit_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["answer"] == "combined answer"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    child_id = next(
        record["data"]["child_run_id"]
        for record in records
        if record["name"] == "sub_agent_started"
    )
    with pytest.raises(SystemExit) as replay:
        main(["trace", "replay", str(trace), "--run-id", child_id, "--json"])
    assert replay.value.code == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["run_id"] == child_id
    assert replayed["status"] == "finished"


@pytest.mark.e2e
def test_disabling_team_profile_keeps_single_agent_runnable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    (tmp_path / "minidsh.toml").write_text(
        "[profiles.team]\nmulti_agent_enabled = true\n"
        'enabled_tools = ["read_file", "delegate_sub_agents"]\n',
        encoding="utf-8",
    )
    script = tmp_path / "single.json"
    script.write_text('[{"content":"single agent"}]', encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "task",
                "--workspace",
                str(tmp_path),
                "--script",
                str(script),
                "--profile",
                "team",
                "--no-multi-agent",
            ]
        )
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["answer"] == "single agent"


def test_gui_key_is_forwarded_to_parent_and_subagents_without_changing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    received: list[str] = []
    key = "synthetic_gui_credential"

    class FakeModel:
        def __init__(self, *, parent: bool) -> None:
            responses = [ModelResponse(f"child result {key}")]
            if parent:
                responses = [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "delegate",
                                "delegate_sub_agents",
                                {
                                    "tasks": [{"name": "reviewer", "task": "inspect"}],
                                },
                            ),
                        )
                    ),
                    ModelResponse("done"),
                ]
            self.script = ScriptedModel(responses)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            return await self.script.complete(request)

    def provider(
        profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
    ) -> FakeModel:
        del temperature, max_attempts
        received.append(profile.api_key(environ))
        return FakeModel(parent=len(received) == 1)

    async def serve(session: GuiSession, *, port: int, open_browser: bool) -> None:
        del port, open_browser
        await session.action(
            "settings",
            {
                "multi_agent_enabled": True,
                "subagent_max_agents": 4,
                "subagent_max_parallel": 2,
                "api_key": key,
            },
        )
        await session.action("run", {"task": "inspect"})
        await session.wait()
        assert session.snapshot()["status"] == "finished"
        assert session.snapshot()["sub_agents"][0]["answer"] == "child result [REDACTED]"
        assert key not in str(session.snapshot())
        for artifact in (tmp_path / ".minidsh").rglob("*"):
            if artifact.is_file():
                assert key not in artifact.read_text(encoding="utf-8")

    monkeypatch.setattr("minidsh.cli.build_provider_model", provider)
    monkeypatch.setattr("minidsh.cli.serve_gui", serve)
    with pytest.raises(SystemExit) as exit_info:
        main(["gui", "--workspace", str(tmp_path), "--model-profile", "deepseek", "--no-open"])
    assert exit_info.value.code == 0
    assert received == [key, key]
    assert "DEEPSEEK_API_KEY" not in os.environ

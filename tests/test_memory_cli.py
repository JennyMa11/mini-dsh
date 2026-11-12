"""Ordinary maintenance is model-free; consolidation is an explicit isolated run."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict

import pytest

from minidsh.cli import main
from minidsh.domain import JsonValue

pytestmark = pytest.mark.integration


class _Receipt(TypedDict):
    status: str
    result: dict[str, JsonValue]


def _configuration(root: Path) -> tuple[Path, Path, Path]:
    workspace, private = root / "workspace", root / "private"
    workspace.mkdir()
    private.mkdir()
    (private / "project.toml").write_text("use_uv=true\n")
    raw = json.dumps(
        {
            "schema_version": "1.0",
            "kind": "operator_configuration",
            "project_id": "alpha",
            "applicable_version": "v1",
            "source_run_id": "manual_run",
            "configuration": {
                "path": "project.toml",
                "sha256": hashlib.sha256((private / "project.toml").read_bytes()).hexdigest(),
            },
            "confirmed_by": "fixture_operator",
            "confirmed_at": "2026-01-01T00:00:00+00:00",
        },
        sort_keys=True,
    ).encode()
    (private / "bundle.json").write_bytes(raw)
    config = workspace / "memory.toml"
    config.write_text(f'''[memory]
project_id="alpha"
applicable_version="v1"
database={json.dumps(str(private / "memory.sqlite3"))}
storage_root={json.dumps(str(private))}
evidence_root={json.dumps(str(private))}
retrieval_mode="progressive_v2"
[[memory.evidence_grants]]
evidence_id="manual"
[memory.evidence_grants.document]
path="bundle.json"
sha256="{hashlib.sha256(raw).hexdigest()}"
''')
    return workspace, private, config


def _invoke(  # noqa: PLR0913 - explicit CLI workspace, config and expected result
    capsys: pytest.CaptureFixture[str],
    workspace: Path,
    config: Path,
    action: str,
    arguments: dict[str, object] | None = None,
    *,
    expected: int = 0,
    script: Path | None = None,
) -> _Receipt:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "memory",
                action,
                "--workspace",
                str(workspace),
                "--config",
                str(config),
                "--operator",
                "fixture_operator",
                "--arguments",
                json.dumps(arguments or {}),
                *(["--script", str(script)] if script is not None else []),
            ]
        )
    assert caught.value.code == expected
    output = capsys.readouterr()
    assert not output.err
    result: JsonValue = json.loads(output.out)
    assert isinstance(result, dict)
    trace_name = result["trace_file"]
    assert isinstance(trace_name, str)
    assert Path(trace_name).name == trace_name
    operator_trace = workspace.parent / "memory_operator_trace.jsonl"
    with operator_trace.open("a") as handle:
        handle.write((workspace.parent / "private" / trace_name).read_text())
    status, payload = result["status"], result["result"]
    assert isinstance(status, str)
    assert payload is None or isinstance(payload, dict)
    return {"status": status, "result": payload or {}}


def test_c08_cli_consolidation_uses_explicit_model_and_private_audit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def provider_selection_forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("explicit offline script must bypass provider selection")

    monkeypatch.setenv("MINIDSH_MODEL_PROFILE", "deepseek")
    monkeypatch.setattr("minidsh.cli._select_model", provider_selection_forbidden)
    workspace, private, config = _configuration(tmp_path)
    _invoke(capsys, workspace, config, "migrate")
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "draft",
                            "name": "propose_consolidated_note",
                            "arguments": {
                                "kind": "environment",
                                "topic": "command_policy",
                                "trigger": "For commands.",
                                "rule": "Prefer the configured project runner.",
                                "verification": "Run explicit checks.",
                            },
                        }
                    ]
                },
                {"content": "Review the proposed note."},
            ]
        )
    )
    fact = "The project command completed in its configured environment."
    receipt = _invoke(
        capsys,
        workspace,
        config,
        "consolidate",
        {
            "materials_authorized": True,
            "redacted": True,
            "facts": [
                {
                    "fact_id": "command",
                    "source_run_id": "actor_run",
                    "content": fact,
                    "content_sha256": hashlib.sha256(fact.encode()).hexdigest(),
                }
            ],
        },
        script=script,
    )
    assert receipt["status"] == "succeeded"
    assert receipt["result"]["status"] == "finished"
    run_id = receipt["result"]["run_id"]
    assert isinstance(run_id, str)
    candidates = receipt["result"]["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    assert isinstance(candidates[0], dict)
    assert candidates[0]["status"] == "proposed"
    assert candidates[0]["approval_sha256"] is None
    assert (private / ".minidsh/sessions" / f"{run_id}.json").is_file()
    assert not (workspace / ".minidsh").exists()


def test_n12_cli_migration_never_initializes_a_model_and_saves_operator_trace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, private, config = _configuration(tmp_path)

    def forbidden_model(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("operator maintenance must not construct a model")

    monkeypatch.setattr("minidsh.cli.build_provider_model", forbidden_model)
    result = _invoke(capsys, workspace, config, "migrate")
    assert result["status"] == "succeeded"
    assert result["result"]["to_version"] == 2
    assert list(private.glob("memory-operator-*.jsonl"))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize((private / "memory.sqlite3").read_bytes())
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert set(workspace.iterdir()) == {config}


def test_n12_cli_review_and_explicit_approval_commit_a_structured_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, private, config = _configuration(tmp_path)
    _invoke(capsys, workspace, config, "migrate")
    draft = _invoke(
        capsys,
        workspace,
        config,
        "merge",
        {
            "entry_id": "manual_note",
            "kind": "environment",
            "topic": "uv_rule",
            "source_run_id": "manual_run",
            "trigger": "For commands.",
            "rule": "Use uv.",
            "verification": "Run the command contract.",
        },
    )
    assert draft["result"]["status"] == "proposed"
    reviewed = _invoke(capsys, workspace, config, "review", {"entry_id": "manual_note"})
    content = reviewed["result"]["content"]
    assert isinstance(content, str)
    assert json.loads(content)["rule"] == "Use uv."
    _invoke(
        capsys,
        workspace,
        config,
        "validate",
        {"entry_id": "manual_note", "expected_revision": 1, "evidence_id": "manual"},
    )
    approved = _invoke(
        capsys,
        workspace,
        config,
        "approve",
        {
            "entry_id": "manual_note",
            "expected_revision": 1,
            "content_sha256": draft["result"]["content_sha256"],
            "approved_at": "2026-01-01T00:00:00+00:00",
        },
    )
    committed = _invoke(
        capsys,
        workspace,
        config,
        "commit",
        {
            "entry_id": "manual_note",
            "expected_revision": 1,
            "approval_sha256": approved["result"]["approval_sha256"],
        },
    )
    assert committed["result"]["status"] == "committed"
    history = _invoke(capsys, workspace, config, "history", {"entry_id": "manual_note"})
    records = history["result"]["records"]
    assert isinstance(records, list)
    assert isinstance(records[0], dict)
    assert records[0]["operation"] == "created"
    assert history["result"]["next_after"] is not None
    assert (private / "memory.sqlite3").exists()


def test_n12_cli_invalid_arguments_do_not_create_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, private, config = _configuration(tmp_path)
    result = _invoke(
        capsys,
        workspace,
        config,
        "approve",
        {
            "entry_id": "manual_note",
            "expected_revision": True,
            "content_sha256": "a" * 64,
            "approved_at": "2026-01-01T00:00:00+00:00",
        },
        expected=2,
    )
    assert result["status"] == "invalid"
    assert not (private / "memory.sqlite3").exists()


def test_n12_cli_deny_rule_overrides_explicit_operator_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, private, config = _configuration(tmp_path)
    config.write_text('denied_tools=["migrate_memory_history"]\n' + config.read_text())
    result = _invoke(capsys, workspace, config, "migrate", expected=2)
    assert result["status"] == "denied"
    assert not (private / "memory.sqlite3").exists()

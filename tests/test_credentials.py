"""Credential precedence, private persistence and model-tool isolation."""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.credentials import CredentialStore, default_credentials_path
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.context.retrieval import PythonSymbolRetriever, RepositoryRetriever
from minidsh.domain import JsonValue, ToolCall, ToolStatus


def _store(tmp_path: Path) -> CredentialStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return CredentialStore(workspace, tmp_path / "global" / "credentials.toml")


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[credentials]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
    )


def test_global_path_default_override_and_relative_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINIDSH_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_credentials_path() == tmp_path / ".config" / "minidsh" / "credentials.toml"
    directory = tmp_path / "chosen"
    monkeypatch.setenv("MINIDSH_CONFIG_HOME", str(directory))
    assert default_credentials_path() == directory / "credentials.toml"
    monkeypatch.setenv("MINIDSH_CONFIG_HOME", "relative")
    with pytest.raises(ValueError, match="absolute"):
        default_credentials_path()


def test_credentials_precedence_and_no_secret_in_repr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.resolve("DEEPSEEK_API_KEY", {}).value is None
    _write(store.global_path, {"DEEPSEEK_API_KEY": "global-secret"})
    assert store.resolve("DEEPSEEK_API_KEY", {}).value == "global-secret"
    assert store.resolve("DEEPSEEK_API_KEY", {}).source == "global config"
    _write(store.workspace_path, {"DEEPSEEK_API_KEY": "project-secret"})
    selected = store.resolve("DEEPSEEK_API_KEY", {})
    assert selected.value == "project-secret"
    assert selected.source == "workspace config"
    assert "project-secret" not in repr(selected)
    assert (
        store.resolve("DEEPSEEK_API_KEY", {"DEEPSEEK_API_KEY": "env-secret"}).value == "env-secret"
    )


def test_other_provider_and_blank_keys_do_not_hide_global_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store.global_path, {"DEEPSEEK_API_KEY": "default"})
    _write(store.workspace_path, {"DEEPSEEK_API_KEY": " ", "OPENAI_API_KEY": "other"})
    assert store.resolve("DEEPSEEK_API_KEY", {"DEEPSEEK_API_KEY": ""}).value == "default"
    assert store.resolve("OPENAI_API_KEY", {}).value == "other"


@pytest.mark.parametrize(
    "content",
    ["[credentials\nsecret", "[credentials]\nKEY = 42", '[credentials]\n"bad key" = "secret"'],
)
def test_bad_config_fails_without_echoing_values(tmp_path: Path, content: str) -> None:
    store = _store(tmp_path)
    store.global_path.parent.mkdir()
    store.global_path.write_text(content)
    with pytest.raises(ValueError, match="credential") as exc:
        store.resolve("DEEPSEEK_API_KEY", {})
    assert content not in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_save_preserves_other_keys_and_creates_private_ignored_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store.workspace_path, {"OPENAI_API_KEY": "preserve"})
    ignore = store.workspace_path.parent.parent / ".gitignore"
    ignore.write_text("dist/\n")
    path = await store.save_workspace("DEEPSEEK_API_KEY", 'private"\\value')
    assert path == store.workspace_path
    values = tomllib.loads(path.read_text())["credentials"]
    assert values == {"DEEPSEEK_API_KEY": 'private"\\value', "OPENAI_API_KEY": "preserve"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        ignore.read_text()
        == "dist/\n\n# Mini-dsh private credentials\n/.minidsh/credentials.toml\n"
    )
    traces = list((path.parent / "traces").glob("*.jsonl"))
    assert traces
    events = [json.loads(line) for trace in traces for line in trace.read_text().splitlines()]
    assert any(
        event["name"] == "permission_decided" and event["data"]["allowed"] for event in events
    )
    assert any(event["name"] == "tool_call_completed" for event in events)
    for trace in traces:
        assert "preserve" not in trace.read_text()
        assert "private" not in trace.read_text()
    await store.save_workspace("DEEPSEEK_API_KEY", "replacement")
    assert ignore.read_text().count("/.minidsh/credentials.toml") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [".minidsh", ".minidsh/credentials.toml", ".gitignore"])
async def test_save_rejects_symlinks_without_changing_external_files(
    tmp_path: Path, target: str
) -> None:
    store = _store(tmp_path)
    workspace = store.workspace_path.parent.parent
    outside = tmp_path / "outside"
    if target == ".minidsh":
        outside.mkdir()
    else:
        outside.write_text("untouched")
        (workspace / target).parent.mkdir(parents=True, exist_ok=True)
    (workspace / target).symlink_to(outside)
    with pytest.raises(ValueError, match="credential"):
        await store.save_workspace("DEEPSEEK_API_KEY", "new-private-key")
    if outside.is_file():
        assert outside.read_text() == "untouched"
    else:
        assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_model_file_tools_cannot_read_or_overwrite_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store.workspace_path, {"DEEPSEEK_API_KEY": "private-value"})
    runtime = build_local_runtime(
        workspace=store.workspace_path.parent.parent,
        model=ScriptedModel([]),
        options=RuntimeOptions(allow_write=True, progressive_tools_enabled=True),
    )
    probes: tuple[tuple[str, dict[str, JsonValue]], ...] = (
        ("read_file", {"path": ".minidsh/credentials.toml"}),
        (
            "write_file",
            {"path": ".minidsh/credentials.toml", "content": "overwrite", "overwrite": True},
        ),
        (
            "patch_file",
            {"path": ".minidsh/credentials.toml", "expected": "private", "replacement": "bad"},
        ),
    )
    for tool, arguments in probes:
        observation = await runtime.tools.execute(ToolCall(tool, tool, arguments), "probe", 1)
        assert observation.status is ToolStatus.FAILED
        assert observation.error is not None
        assert observation.error.code == "sandbox_violation"
        assert "private-value" not in str(observation)
    assert store.resolve("DEEPSEEK_API_KEY", {}).value == "private-value"


@pytest.mark.parametrize("retriever", [RepositoryRetriever, PythonSymbolRetriever])
def test_retrieval_does_not_read_a_symlink_to_credentials(
    tmp_path: Path, retriever: type[RepositoryRetriever] | type[PythonSymbolRetriever]
) -> None:
    store = _store(tmp_path)
    store.workspace_path.parent.mkdir()
    store.workspace_path.write_text('def private_key():\n    return "sensitive-value"\n')
    alias = store.workspace_path.parent.parent / "alias.py"
    alias.symlink_to(store.workspace_path)
    assert retriever(alias.parent).search("private_key", limit=5) == ()
